cmake_minimum_required(VERSION 3.16)

# S2 uses the ROM USB-OTG CDC driver; keep its pinned compatibility fix
# scoped to this target so other chips retain their SDK components.
if(IDF_TARGET STREQUAL "esp32s2")
    list(APPEND EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/platform/esp32s2/esp_usb_cdc_rom_console")
endif()

option(ESP_SDR_STREAMING "Build the S31 Ethernet/high-speed USB receiver" OFF)
if(ESP_SDR_STREAMING)
    if(NOT IDF_TARGET STREQUAL "esp32s31")
        message(FATAL_ERROR "ESP_SDR_STREAMING requires ESP32-S31")
    endif()
    list(APPEND EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/main/targets/esp32s31/streaming")
else()
    set(ENV{IDF_COMPONENT_MANAGER} "0")
endif()
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
idf_build_set_property(MINIMAL_BUILD ON)
idf_build_set_property(ESP_SDR_STREAMING "${ESP_SDR_STREAMING}")
project(esp_sdr)

# Local workaround for hosts with a tight per-process memory limit: the GCC
# debug-info generator is the single largest consumer while compiling the
# C++ sources of esp-dsp, and dropping it keeps cc1plus inside the limit.
# Enable with -DESP_SDR_SLIM_BUILD=ON (never on for release builds).
option(ESP_SDR_SLIM_BUILD "Drop debug info to reduce compiler memory" OFF)
if(ESP_SDR_SLIM_BUILD)
    add_compile_options(-g0)
endif()
