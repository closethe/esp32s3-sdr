/* Transmit backend for the ESP32-S3 burst backend.
 *
 * The dump engine behind burst capture is bidirectional. Its transmit half
 * lives at 0x60033d64 and reads words straight out of the 64 KiB aperture at
 * 0x3fcd0000, so the CPU never touches the DAC path sample by sample. This is
 * the same path the original ESP-SDR receive/transmit firmware used before it
 * was removed; see commit e605684 for the reference implementation.
 */
#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_cpu.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "soc/soc.h"

#include "burst_serial.h"
#include "tx.h"

#define TX_WORDS       16380u
#define TX_BUFFER      ((uint32_t *)0x3fcd0000)
#define SRAM_OWNER_REG 0x600c101cu
#define RX_CTRL_REG    0x60033d5cu
#define TX_CTRL_REG    0x60033d64u

#define TX_LEASE_US    5000000ll

/* Vendor RF test library: powers the TX chain up/down for a test tone.
 * Signature recovered from librftest.a:esp_phy_api.o (thin wrapper, three
 * arguments: enable, tone frequency selector, amplitude index). */
extern void esp_phy_wifi_tx_tone(unsigned enable, unsigned tone, unsigned amplitude);
extern void stop_tx_tone(unsigned);
#define phy_stop_tx_tone stop_tx_tone

static s3_tx_hooks_t hooks;
static portMUX_TYPE tx_mux = portMUX_INITIALIZER_UNLOCKED;

static bool replay_active;
static unsigned replay_samples;
static unsigned replay_rate;
static int64_t replay_deadline;
static uint32_t replay_owner;

static bool cw_active;
static int64_t cw_deadline;

static void reply(const char *s) {
    (void)burst_serial_send(s, strlen(s));
}

void s3_tx_bind(const s3_tx_hooks_t *h) {
    hooks = *h;
}

/* ---- wire formats ------------------------------------------------------ */

static size_t packed_size(unsigned n) { return (n * 20u + 7u) / 8u; }

static size_t wire_size(unsigned n, unsigned format) {
    return format == 16 ? (size_t)n * 2u : packed_size(n);
}

/* IQ10 packed: two samples in five bytes, I in bits 0..9, Q in bits 10..19. */
static void unpack_iq(unsigned n) {
    const uint8_t *base = (const uint8_t *)TX_BUFFER;
    for (unsigned pair = (n + 1) / 2; pair-- > 0;) {
        const uint8_t *p = base + pair * 5u;
        uint32_t a = p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)(p[2] & 15u) << 16);
        if (pair * 2u + 1u < n) {
            uint32_t b = (p[2] >> 4) | ((uint32_t)p[3] << 4) | ((uint32_t)p[4] << 12);
            TX_BUFFER[pair * 2u + 1u] = b;
        }
        TX_BUFFER[pair * 2u] = a;
    }
}

/* IQ8: signed bytes I,Q per sample. Each byte is the top eight bits of the
 * corresponding 10-bit field, so the two low bits are restored as zero. */
static void unpack_iq8(unsigned n) {
    const uint8_t *p = (const uint8_t *)TX_BUFFER;
    for (unsigned j = n; j-- > 0;) {
        uint32_t i = p[2u * j];
        uint32_t q = p[2u * j + 1u];
        TX_BUFFER[j] = (i << 2) | (q << 12);
    }
}

/* ---- dump engine, TX direction ----------------------------------------- */

static bool IRAM_ATTR emit(unsigned n, unsigned rate, unsigned repeats) {
    uint32_t owner = REG_READ(SRAM_OWNER_REG);
    REG_WRITE(RX_CTRL_REG, 0);
    REG_WRITE(TX_CTRL_REG, 0);
    uint32_t ctrl = 0x80000000u | (n & 0x3fffu);
    if (rate == 80000000u) ctrl |= 1u << 15;
    if (repeats > 1) ctrl |= (1u << 19) | ((repeats & 0xffu) << 20);
    uint32_t start;
    bool done;
    const uint32_t limit = (240000000u / rate) * n * (repeats ? repeats : 1u) + 240000u;

    taskENTER_CRITICAL(&tx_mux);
    REG_WRITE(SRAM_OWNER_REG, (owner & ~15u) | 4u);
    start = esp_cpu_get_cycle_count();
    REG_WRITE(TX_CTRL_REG, ctrl);
    do {
        done = (REG_READ(TX_CTRL_REG) & (1u << 18)) != 0;
    } while (!done && esp_cpu_get_cycle_count() - start < limit);
    REG_WRITE(TX_CTRL_REG, 0);
    REG_WRITE(SRAM_OWNER_REG, owner);
    taskEXIT_CRITICAL(&tx_mux);
    return done;
}

static void radio_warm_up(void) {
    hooks.prepare_rx();
    esp_phy_wifi_tx_tone(1, 1, 80);
    hooks.tune(hooks.frequency_mhz());
    phy_stop_tx_tone(1);
    hooks.invalidate_rx();
}

static void radio_cool_down(void) {
    esp_phy_wifi_tx_tone(0, 1, 80);
}

/* ---- single-shot transmit ---------------------------------------------- */

static bool upload(unsigned n, unsigned format, uint32_t crc) {
    size_t bytes = wire_size(n, format);
    if (burst_serial_read(TX_BUFFER, bytes, 5000000) != bytes) {
        reply("ERR upload_timeout\n");
        return false;
    }
    if (esp_rom_crc32_le(0, (const uint8_t *)TX_BUFFER, bytes) != crc) {
        reply("ERR crc\n");
        return false;
    }
    if (format == 16) unpack_iq8(n); else unpack_iq(n);
    return true;
}

static bool transmit(unsigned n, unsigned rate, uint32_t crc, unsigned format, unsigned repeats) {
    reply("READY\n");
    if (!upload(n, format, crc)) return false;
    hooks.prepare_rx();
    if (!hooks.gain_ready()) {
        reply("ERR gain\n");
        return false;
    }
    /* Keep the analogue filter configuration the host selected. */
    hooks.filter_apply();
    radio_warm_up();
    uint32_t start = esp_cpu_get_cycle_count();
    bool ok = emit(n, rate, repeats);
    uint32_t cycles = esp_cpu_get_cycle_count() - start;
    radio_cool_down();
    hooks.filter_restore();
    if (!ok) {
        reply("ERR dma_timeout\n");
        return false;
    }
    char h[96];
    snprintf(h, sizeof(h), "SENT %" PRIu32 " %" PRIu32 " %" PRIu32 "\n",
             (uint32_t)n * (uint32_t)repeats, cycles, (uint32_t)0);
    reply(h);
    return true;
}

/* ---- continuous replay -------------------------------------------------- */

static void replay_stop(void) {
    if (!replay_active) return;
    taskENTER_CRITICAL(&tx_mux);
    REG_WRITE(TX_CTRL_REG, 0);
    REG_WRITE(SRAM_OWNER_REG, replay_owner);
    taskEXIT_CRITICAL(&tx_mux);
    esp_phy_wifi_tx_tone(0, 1, 80);
    replay_active = false;
    hooks.invalidate_rx();
    hooks.prepare_rx();
}

static void replay_start(unsigned n, unsigned rate, uint32_t crc, unsigned format) {
    replay_stop();
    cw_active = false;
    hooks.prepare_rx();
    reply("READY\n");
    if (!upload(n, format, crc)) return;
    radio_warm_up();
    replay_owner = REG_READ(SRAM_OWNER_REG);
    /* Count zero plus the loop bit selects unlimited modem-local replay. */
    uint32_t ctrl = 0x80080000u | (n & 0x3fffu);
    if (rate == 80000000u) ctrl |= 1u << 15;
    taskENTER_CRITICAL(&tx_mux);
    REG_WRITE(RX_CTRL_REG, 0);
    REG_WRITE(TX_CTRL_REG, 0);
    REG_WRITE(SRAM_OWNER_REG, (replay_owner & ~15u) | 4u);
    REG_WRITE(TX_CTRL_REG, ctrl);
    taskEXIT_CRITICAL(&tx_mux);
    replay_active = true;
    replay_samples = n;
    replay_rate = rate;
    replay_deadline = esp_timer_get_time() + TX_LEASE_US;
    reply("PLAYING\n");
}

/* ---- continuous wave ---------------------------------------------------- */

static void cw_stop(void) {
    if (!cw_active) return;
    esp_phy_wifi_tx_tone(0, 1, 80);
    cw_active = false;
    hooks.invalidate_rx();
    hooks.prepare_rx();
}

/* ---- command set -------------------------------------------------------- */

static bool replay_command(const char *line) {
    if (!strcmp(line, "REPLAY STOP")) { replay_stop(); reply("OK\n"); return true; }
    if (!strcmp(line, "REPLAY KEEP")) {
        if (!replay_active) reply("ERR replay_inactive\n");
        else { replay_deadline = esp_timer_get_time() + TX_LEASE_US; reply("OK\n"); }
        return true;
    }
    if (!strcmp(line, "REPLAY?")) {
        char h[64];
        snprintf(h, sizeof(h), "REPLAY %s %u %u\n", replay_active ? "ON" : "OFF", replay_samples, replay_rate);
        reply(h);
        return true;
    }
    unsigned n, rate, crc;
    char extra;
    bool iq8 = !strncmp(line, "REPLAY16 ", 9);
    if ((iq8 ? sscanf(line, "REPLAY16 %u %u %x %c", &n, &rate, &crc, &extra)
             : sscanf(line, "REPLAY20 %u %u %x %c", &n, &rate, &crc, &extra)) == 3 &&
        n > 0 && n <= TX_WORDS && (rate == 40000000u || rate == 80000000u)) {
        replay_start(n, rate, crc, iq8 ? 16 : 20);
        return true;
    }
    if (replay_active && strcmp(line, "INFO") && strcmp(line, "CAPS") && strcmp(line, "GAIN?"))
        replay_stop();
    return false;
}

static bool cw_command(const char *line) {
    if (!strcmp(line, "CW STOP")) { cw_stop(); reply("OK\n"); return true; }
    if (!strcmp(line, "CW KEEP")) {
        if (!cw_active) reply("ERR cw_inactive\n");
        else { cw_deadline = esp_timer_get_time() + TX_LEASE_US; reply("OK\n"); }
        return true;
    }
    if (!strcmp(line, "CW START")) {
        if (!cw_active) {
            replay_stop();
            hooks.prepare_rx();
            esp_phy_wifi_tx_tone(1, 1, 80);
            hooks.tune(hooks.frequency_mhz());
            phy_stop_tx_tone(1);
            hooks.invalidate_rx();
            cw_active = true;
        }
        cw_deadline = esp_timer_get_time() + TX_LEASE_US;
        reply("OK\n");
        return true;
    }
    if (!strcmp(line, "CW?")) { reply(cw_active ? "CW ON\n" : "CW OFF\n"); return true; }
    if (cw_active && strcmp(line, "INFO") && strcmp(line, "CAPS") && strcmp(line, "GAIN?")) cw_stop();
    return false;
}

bool s3_tx_command(char *line) {
    if (replay_command(line)) return true;
    if (cw_command(line)) return true;

    unsigned n, rate, repeats, format;
    char extra;
    bool iq8 = false;
    if (!strncmp(line, "TX16 ", 5)) { iq8 = true; memcpy(line, "TX20", 4); }
    if (!strncmp(line, "LOOP16 ", 7)) { iq8 = true; memcpy(line, "LOOP20", 6); }
    uint32_t crc;
    int k = (!strncmp(line, "TX20 ", 5) ? sscanf(line, "TX20 %u %u %" SCNx32 " %c", &n, &rate, &crc, &extra)
                                          : sscanf(line, "TX %u %u %" SCNx32 " %c", &n, &rate, &crc, &extra));
    if ((line[0] == 'T') && k == 3 &&
        n > 0 && n <= TX_WORDS && (rate == 40000000u || rate == 80000000u) && n <= rate / 10u) {
        (void)transmit(n, rate, crc, iq8 ? 16 : 20, 1);
        return true;
    }
    if (sscanf(line, "TXRUN %u %u %u %u %c", &n, &rate, &repeats, &format, &extra) == 4 &&
        n > 0 && n <= TX_WORDS && repeats > 0 && repeats <= 1000 &&
        (format == 16 || format == 20) && (rate == 40000000u || rate == 80000000u)) {
        reply("RUN\n");
        for (unsigned j = 0; j < repeats; j++) {
            uint8_t h[4];
            if (burst_serial_read(h, 4, 5000000) != 4) { reply("ERR upload_timeout\n"); return true; }
            uint32_t checksum = h[0] | ((uint32_t)h[1] << 8) | ((uint32_t)h[2] << 16) | ((uint32_t)h[3] << 24);
            if (!transmit(n, rate, checksum, format, 1)) return true;
        }
        reply("END\n");
        return true;
    }
    /* LOOP takes <n> <rate> <repeats> <crc>; the host tool omits repeats. */
    bool lo = false;
    if (sscanf(line, "LOOP20 %u %u %u %" SCNx32 " %c", &n, &rate, &repeats, &crc, &extra) == 4) {
        lo = repeats == 1;
    }
    if (!lo) {
        repeats = 1;
        lo = sscanf(line, "LOOP20 %u %u %" SCNx32 " %c", &n, &rate, &crc, &extra) == 3;
    }
    if (lo && n > 0 && n <= TX_WORDS && (rate == 40000000u || rate == 80000000u)) {
        unsigned fmt = iq8 ? 16 : 20;
        if (!transmit(n, rate, crc, fmt, 1)) return true;
        (void)hooks.capture(n, rate == 40000000u ? 1 : 0, fmt);
        return true;
    }
    return false;
}

void s3_tx_idle(void) {
    if (replay_active && esp_timer_get_time() >= replay_deadline) replay_stop();
    if (cw_active && esp_timer_get_time() >= cw_deadline) cw_stop();
}

void s3_tx_release(void) {
    replay_stop();
    cw_stop();
}
