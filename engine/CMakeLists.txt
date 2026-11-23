# Settings shared by every engine target. Warnings are strict, and floating point is never
# contracted into fused multiply-adds, so arm64 (MacBook) and x86-64 (Linux) round alike.
add_library(engine_options INTERFACE)
target_compile_options(engine_options INTERFACE
  -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion -Wshadow
  -ffp-contract=off
  $<$<BOOL:${ENGINE_WERROR}>:-Werror>)

if(ENGINE_CLANG_TIDY)
  find_program(CLANG_TIDY_EXE NAMES clang-tidy REQUIRED)
endif()

# Applies the shared settings to one of our own targets, and clang-tidy when asked for.
function(engine_target target)
  target_link_libraries(${target} PRIVATE engine_options)
  if(ENGINE_CLANG_TIDY)
    set_target_properties(${target} PROPERTIES CXX_CLANG_TIDY "${CLANG_TIDY_EXE}")
  endif()
endfunction()

# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
include(GoogleTest)

add_executable(engine_tests
  tests/smoke_test.cpp)
target_link_libraries(engine_tests PRIVATE simdjson::simdjson ZLIB::ZLIB GTest::gtest_main)
target_link_libraries(engine_tests PRIVATE engine_options)
gtest_discover_tests(engine_tests)

# A test written before the code it tests goes in tests/red/. Those tests carry the label
# "red": the dev preset skips them, and CI fails while any remain.
file(GLOB red_sources CONFIGURE_DEPENDS tests/red/*.cpp)
if(red_sources)
  add_executable(engine_red_tests ${red_sources})
  target_link_libraries(engine_red_tests PRIVATE simdjson::simdjson ZLIB::ZLIB GTest::gtest_main)
  target_link_libraries(engine_red_tests PRIVATE engine_options)
  gtest_discover_tests(engine_red_tests PROPERTIES LABELS red)
endif()
