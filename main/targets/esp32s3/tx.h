/* ESP32-S3 transmit backend: the reverse direction of the same dump engine
 * that burst capture uses. CPU fills the 64 KiB aperture, the hardware reads
 * it back out through the TX baseband/DAC. Receive-only builds do not include
 * this file. */
#pragma once

#include <stdbool.h>

typedef struct {
    void (*prepare_rx)(void);
    void (*tune)(unsigned mhz);
    unsigned (*frequency_mhz)(void);
    void (*invalidate_rx)(void);
    bool (*gain_ready)(void);
    void (*filter_apply)(void);
    void (*filter_restore)(void);
    bool (*capture)(unsigned n, unsigned divider, unsigned format);
} s3_tx_hooks_t;

void s3_tx_bind(const s3_tx_hooks_t *hooks);
/* Returns true when the line belonged to the transmit command set. */
bool s3_tx_command(char *line);
/* Lease expiry for CW / replay; call from the serial idle path. */
void s3_tx_idle(void);
/* Host released the port: stop every continuous transmitter. */
void s3_tx_release(void);
