# The C++ engine. One command configures, builds and tests it:
#
#     cmake --workflow --preset dev
#
# The presets in CMakePresets.json choose the options below.
cmake_minimum_required(VERSION 3.25)
project(kalshi_settlement_engine LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(ENGINE_WERROR "Treat warnings in the engine's own code as errors" OFF)
option(ENGINE_SANITIZE "Build with the address and undefined-behaviour sanitizers" OFF)
option(ENGINE_CLANG_TIDY "Run clang-tidy on the engine's own code" OFF)
option(ENGINE_BENCHMARKS "Build the benchmarks (fetches Google Benchmark)" OFF)

if(ENGINE_SANITIZE)
  # Added before the dependencies, so their code is checked too.
  add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer
                      -fno-sanitize-recover=all)
  add_link_options(-fsanitize=address,undefined)
endif()

include(cmake/dependencies.cmake)

enable_testing()
add_subdirectory(engine)
