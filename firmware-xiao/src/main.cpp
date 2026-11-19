/*
 * eve — firmware for the Seeed Studio XIAO ESP32S3 Sense.
 *
 * A port of ../firmware/src/main.cpp (Atech Tiny) to off-the-shelf parts:
 * the same recording and tap logic, and the same USB protocol, so
 * host/voicetype.py works unchanged with either stick.
 *
 * Hardware:
 *   Mic     PDM microphone on the Sense board   CLK=GPIO42  DATA=GPIO41
 *   Button  tactile switch, D1 (GPIO2) to GND    internal pull-up
 *           (build env xiao-boot uses the BOOT button, GPIO0, instead)
 *   Light   onboard user LED, GPIO21, active LOW (single colour, so the
 *           Atech RGB colours become blink patterns, see below)
 *
 * Wire protocol, device -> host (binary, over USB-CDC):
 *   [0xA5 0x5A] [type:1] [len:2, little endian] [payload:len]
 *     'S'  recording started            (len 0)
 *     'D'  audio, int16 LE mono 16 kHz  (len <= 1024, always even)
 *     'E'  recording ended              (len 4: uint32 dropped-sample count)
 *     'A'  recording aborted — it was only a tap; discard it (len 0)
 *     'T'  single tap                   (len 0)  host: Enter if text waiting, else Tab
 *     'N'  double tap                   (len 0)  host: delete the waiting text
 *
 * Host -> device (single bytes, drive the light):
 *   'B' busy/transcribing   'K' done   'X' error   'I' idle
 *
 * Light patterns:
 *   3 blinks at power-up   firmware booted
 *   solid on               listening
 *   fast blink             finishing the last phrase (busy)
 *   one long blink         typed
 *   one short blink        tap (Enter / Tab)
 *   two short blinks       double tap (delete)
 *   five rapid blinks      something went wrong (see the host log)
 */

#include <Arduino.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/stream_buffer.h"
#include "driver/i2s.h"
#include "esp_task_wdt.h"

// ---- pins -------------------------------------------------------------------
#ifndef BTN_PIN
#define BTN_PIN D1          // 12 mm button to GND. -DBTN_PIN=0 for the BOOT button
#endif
static const int MIC_CLK = 42;
static const int MIC_DATA = 41;
static const int LED_PIN = 21;   // active LOW

// ---- tuning (same as the Atech firmware) ------------------------------------
static const uint32_t TAIL_MS        = 300;     // keep recording after release so the last word isn't clipped
static const uint32_t MAX_RECORD_MS  = 120000;  // hard cap on one dictation
static const uint32_t HOST_WAIT_MS   = 4000;    // busy light gives up if the host never answers
static const uint32_t TAP_MS         = 250;     // a press shorter than this is a tap, not dictation
static const uint32_t DOUBLE_TAP_MS  = 350;     // max gap from releasing tap 1 to pressing tap 2
static const uint32_t DEBOUNCE_MS    = 15;
static const size_t   STREAM_BYTES   = 16384;   // ~0.5 s of audio buffered between mic task and USB
static const size_t   PACKET_BYTES   = 1024;    // audio bytes per 'D' packet
static const int      SAMPLE_RATE    = 16000;
static const size_t   MIC_CHUNK      = 320;     // samples per I2S read (20 ms)

TaskHandle_t mainTaskHandle = NULL;
TaskHandle_t micTaskHandle  = NULL;

// ---- shared with the mic capture task (core 0) ------------------------------
static StreamBufferHandle_t audioStream = NULL;
static volatile bool        capturing    = false;
static volatile uint32_t    droppedSamples = 0;

// ---- microphone -------------------------------------------------------------
static bool micBegin() {
    i2s_config_t cfg = {};
    cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM);
    cfg.sample_rate = SAMPLE_RATE;
    cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
    cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
    cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
    cfg.dma_buf_count = 8;
    cfg.dma_buf_len = 256;
    cfg.use_apll = false;

    i2s_pin_config_t pins = {};
    pins.mck_io_num = I2S_PIN_NO_CHANGE;
    pins.bck_io_num = I2S_PIN_NO_CHANGE;
    pins.ws_io_num = MIC_CLK;          // in PDM mode the clock goes on WS
    pins.data_out_num = I2S_PIN_NO_CHANGE;
    pins.data_in_num = MIC_DATA;

    if (i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL) != ESP_OK) return false;
    if (i2s_set_pin(I2S_NUM_0, &pins) != ESP_OK) return false;
    return true;
}

// Reads the mic forever. Always reading keeps the DMA fresh, so the first word
// of a dictation isn't stale; audio is only kept while `capturing`.
static void micTask(void*) {
    static int16_t samples[MIC_CHUNK];
    while (true) {
        size_t got = 0;
        i2s_read(I2S_NUM_0, samples, sizeof(samples), &got, portMAX_DELAY);
        if (!capturing || got == 0) continue;
        got &= ~(size_t)1;
        // All-or-nothing so the stream never holds half a sample.
        if (xStreamBufferSpacesAvailable(audioStream) >= got) {
            xStreamBufferSend(audioStream, samples, got, 0);
        } else {
            droppedSamples += got / 2;
        }
    }
}

// ---- button -----------------------------------------------------------------
static bool     btnDown = false, btnPressedEv = false, btnReleasedEv = false;
static uint32_t btnChanged = 0;

static void buttonUpdate() {
    bool down = digitalRead(BTN_PIN) == LOW;
    btnPressedEv = btnReleasedEv = false;
    if (down != btnDown && millis() - btnChanged > DEBOUNCE_MS) {
        btnDown = down;
        btnChanged = millis();
        if (down) btnPressedEv = true; else btnReleasedEv = true;
    }
}

// ---- USB output -------------------------------------------------------------
// Write everything or give up after a short wait (host not reading). A partial
// packet is harmless: the host resyncs on the next magic header.
static bool writeAll(const uint8_t* p, size_t n) {
    size_t off = 0;
    uint32_t t0 = millis();
    while (off < n) {
        size_t w = Serial.write(p + off, n - off);
        off += w;
        if (w == 0) {
            if (millis() - t0 > 50) return false;
            vTaskDelay(1);
        }
    }
    return true;
}

static bool sendPacket(char type, const uint8_t* payload, uint16_t len) {
    uint8_t hdr[5] = {0xA5, 0x5A, (uint8_t)type, (uint8_t)(len & 0xFF), (uint8_t)(len >> 8)};
    if (!writeAll(hdr, sizeof(hdr))) return false;
    if (len && !writeAll(payload, len)) return false;
    return true;
}

static void drainAudio() {
    static uint8_t chunk[PACKET_BYTES];
    // Bound the work per loop so the watchdog and button stay serviced.
    for (int i = 0; i < 8; i++) {
        size_t avail = xStreamBufferBytesAvailable(audioStream);
        if (avail < 2) return;
        size_t want = min(avail, PACKET_BYTES) & ~(size_t)1;   // keep it even
        size_t got = xStreamBufferReceive(audioStream, chunk, want, 0);
        if (got) sendPacket('D', chunk, (uint16_t)got);
    }
}

// ---- light ------------------------------------------------------------------
enum LightState { L_IDLE, L_RECORDING, L_BUSY, L_FLASH };
static LightState lightState = L_IDLE;
static uint32_t   lightSince = 0;
static uint8_t    flashCount = 0;     // blinks in the current flash
static uint16_t   flashOnMs  = 0;
static const uint16_t FLASH_GAP_MS = 120;

static void led(bool on) { digitalWrite(LED_PIN, on ? LOW : HIGH); }

static void setLight(LightState s) {
    lightState = s;
    lightSince = millis();
    if (s == L_IDLE) led(false);
    if (s == L_RECORDING) led(true);
}

static void flash(uint8_t count, uint16_t onMs) {
    flashCount = count;
    flashOnMs  = onMs;
    setLight(L_FLASH);
}

static void serviceLight() {
    uint32_t age = millis() - lightSince;
    switch (lightState) {
        case L_BUSY:
            led((age / 100) % 2 == 0);              // fast blink
            if (age > HOST_WAIT_MS) setLight(L_IDLE);
            break;
        case L_FLASH: {
            uint32_t period = flashOnMs + FLASH_GAP_MS;
            if (age >= period * flashCount) { setLight(L_IDLE); break; }
            led(age % period < flashOnMs);
            break;
        }
        default: break;
    }
}

static void readHost() {
    while (Serial.available() > 0) {
        int c = Serial.read();
        if (lightState == L_RECORDING) continue;    // recording owns the light
        switch (c) {
            case 'B': setLight(L_BUSY);  break;
            case 'K': flash(1, 400);     break;     // typed
            case 'X': flash(5, 60);      break;     // error
            case 'I': setLight(L_IDLE);  break;
            default: break;
        }
    }
}

// ---- recording state machine (unchanged from the Atech firmware) -------------
static bool     recording   = false;   // between 'S' and 'E'
static bool     tailing     = false;   // released, finishing the tail
static uint32_t recStart    = 0;
static uint32_t releasedAt  = 0;
static uint32_t pressedAt   = 0;
static bool     freshPress  = false;   // this press started the current recording

static void startRecording() {
    capturing = false;
    xStreamBufferReset(audioStream);
    droppedSamples = 0;
    sendPacket('S', nullptr, 0);
    recording = true;
    tailing   = false;
    recStart  = millis();
    capturing = true;
    setLight(L_RECORDING);
}

static void finishRecording() {
    capturing = false;
    drainAudio();
    drainAudio();
    uint32_t dropped = droppedSamples;
    sendPacket('E', (const uint8_t*)&dropped, sizeof(dropped));
    recording = false;
    tailing   = false;
    setLight(L_BUSY);   // the host answers with K / X / I
}

// The whole recording was one short tap: tell the host to throw it away.
static void abortRecording() {
    capturing = false;
    xStreamBufferReset(audioStream);
    sendPacket('A', nullptr, 0);
    recording = false;
    tailing   = false;
    setLight(L_IDLE);
}

// ---- taps -------------------------------------------------------------------
static bool     haveTap     = false;   // a first tap is waiting for its partner
static uint32_t tapReleased = 0;

static void registerTap(uint32_t pressedAt) {
    if (haveTap && pressedAt - tapReleased <= DOUBLE_TAP_MS) {
        haveTap = false;
        sendPacket('N', nullptr, 0);
        flash(2, 80);                  // double tap (delete)
    } else {
        haveTap     = true;            // wait to see if a second tap follows
        tapReleased = millis();
    }
}

static void serviceTaps() {
    if (haveTap && !btnDown && millis() - tapReleased > DOUBLE_TAP_MS) {
        haveTap = false;
        sendPacket('T', nullptr, 0);
        flash(1, 80);                  // single tap (Enter / Tab)
    }
}

// ---- the program --------------------------------------------------------------
void mainTask(void*) {
    esp_task_wdt_add(NULL);

    while (true) {
        esp_task_wdt_reset();
        buttonUpdate();
        readHost();

        if (btnPressedEv) {
            pressedAt = millis();
            if (recording && tailing) {
                tailing    = false;            // re-pressed during the tail: keep going
                freshPress = false;
                setLight(L_RECORDING);
            } else if (!recording) {
                startRecording();              // start at once, so dictation has no lag
                freshPress = true;
            }
        }

        if (btnReleasedEv && recording && !tailing) {
            if (millis() - pressedAt < TAP_MS) {
                if (freshPress) abortRecording();
                else            finishRecording();
                registerTap(pressedAt);
            } else {
                tailing    = true;
                releasedAt = millis();
                haveTap    = false;            // tap-then-hold is dictation, not a tap
            }
        }

        serviceTaps();

        if (recording) {
            drainAudio();
            bool tailDone = tailing && (millis() - releasedAt >= TAIL_MS);
            bool tooLong  = (millis() - recStart) >= MAX_RECORD_MS;
            if (tailDone || tooLong) finishRecording();
        }

        serviceLight();
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

void setup() {
    Serial.setTxBufferSize(8192);   // must precede begin()
    Serial.begin(115200);           // baud is ignored on native USB
    Serial.setTxTimeoutMs(0);       // never block the firmware if the host isn't reading

    pinMode(BTN_PIN, INPUT_PULLUP);
    pinMode(LED_PIN, OUTPUT);
    led(false);

    audioStream = xStreamBufferCreate(STREAM_BYTES, 2);

    if (!micBegin()) {
        // No mic: blink rapidly forever. Is the Sense board clipped on?
        while (true) { led(true); delay(60); led(false); delay(60); }
    }
    xTaskCreatePinnedToCore(micTask, "Mic", 4096, NULL, 5, &micTaskHandle, 0);

    // Boot: three blinks = alive.
    for (int i = 0; i < 3; i++) { led(true); delay(120); led(false); delay(120); }

    esp_task_wdt_init(30, true);
    xTaskCreatePinnedToCore(mainTask, "MainTask", 16384, NULL, 1, &mainTaskHandle, 1);
}

void loop() {
    vTaskDelay(portMAX_DELAY);
}
