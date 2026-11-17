#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum { BURST_SERIAL_USB, BURST_SERIAL_UART, BURST_SERIAL_COUNT } burst_serial_port_t;

void burst_serial_init(void);
/* 1: complete line, -1: overlong line, 0: idle. Each port has its own parser.
 * A complete line selects the port for all subsequent replies and payloads.
 * BAUD? and BAUD <1000000|2000000> are consumed internally. Baud changes
 * last until reset; OK BAUD <rate> is sent at the previous rate. */
int burst_serial_poll_line(char *line, size_t capacity);
burst_serial_port_t burst_serial_port(void);
unsigned burst_serial_baud(void);
bool burst_serial_send(const void *data, size_t size);
/* Write only what fits now; never wait for the transport. */
size_t burst_serial_try_send(const void *data, size_t size);
/* Read exactly what the host uploads for a binary transaction. Returns the
 * number of bytes received before the deadline expired. */
size_t burst_serial_read(void *dst, size_t size, int64_t timeout_us);

/* Hooks for backends with continuous transmitters (weak no-ops by default). */
void burst_serial_idle_hook(void);
void burst_serial_release_hook(void);

/* Consume a stream stop request on the owning port only. */
bool burst_serial_stop_requested(void);

/* Read commands forever and pass each line to `handle`. One port at a time
 * owns the receiver: the other gets "ERR busy" until the owner sends RELEASE
 * or stays silent for five seconds. RELEASE itself is answered here. */
void burst_serial_serve(void (*handle)(char *line));
