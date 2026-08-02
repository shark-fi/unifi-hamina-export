# Top-level ESP-IDF project file.
# Build:  idf.py set-target esp32c6   (or esp32c5)
#         idf.py build flash monitor
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(ftm_ble_anchor)
