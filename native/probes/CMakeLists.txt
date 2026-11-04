cmake_minimum_required(VERSION 3.24)
project(doom_eap_transport LANGUAGES CXX)

if(NOT WIN32 OR NOT MSVC)
    message(FATAL_ERROR "The transport relay requires Windows x64 MSVC")
endif()
if(NOT SENTINEL_CORE_SOURCE)
    message(FATAL_ERROR "Set SENTINEL_CORE_SOURCE to the pinned Core checkout")
endif()

set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
include(ExternalProject)
set(core_build "${CMAKE_CURRENT_BINARY_DIR}/core")
ExternalProject_Add(core_client
    SOURCE_DIR "${SENTINEL_CORE_SOURCE}"
    BINARY_DIR "${core_build}"
    CMAKE_ARGS "-DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}" "-DPython3_EXECUTABLE=${Python3_EXECUTABLE}"
    BUILD_COMMAND "${CMAKE_COMMAND}" --build "${core_build}" --config $<CONFIG> --target sentinel_inspection_client --parallel 2
    INSTALL_COMMAND ""
    BUILD_ALWAYS TRUE
    BUILD_BYPRODUCTS "${core_build}/sentinel_protocol.lib" "${core_build}/sentinel_inspection_client.lib")
add_library(sentinel_protocol STATIC IMPORTED)
set_target_properties(sentinel_protocol PROPERTIES IMPORTED_LOCATION "${core_build}/sentinel_protocol.lib")
add_library(sentinel_inspection_client STATIC IMPORTED)
set_target_properties(sentinel_inspection_client PROPERTIES
    IMPORTED_LOCATION "${core_build}/sentinel_inspection_client.lib"
    INTERFACE_LINK_LIBRARIES "sentinel_protocol;bcrypt")
add_dependencies(sentinel_inspection_client core_client)
add_executable(save_death_probe save_death_probe.cpp)
target_link_libraries(save_death_probe PRIVATE sentinel_inspection_client)
target_include_directories(save_death_probe PRIVATE
    "${core_build}/protocol_generated/include" "${SENTINEL_CORE_SOURCE}/include" "${SENTINEL_CORE_SOURCE}/src")
target_compile_features(save_death_probe PRIVATE cxx_std_17)
target_compile_definitions(save_death_probe PRIVATE NOMINMAX _CRT_SECURE_NO_WARNINGS)
