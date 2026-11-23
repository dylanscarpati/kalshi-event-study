# Third-party code. Each library is fetched at a release tag with a shallow git clone,
# then the commit it checked out is compared with the one pinned here. A tag that was
# moved upstream therefore stops the build instead of silently changing the code.
# SYSTEM marks their headers as system headers, so our strict warnings skip them.
include(FetchContent)

function(fetch_pinned name repository tag commit)
  FetchContent_Declare(${name} GIT_REPOSITORY ${repository} GIT_TAG ${tag} GIT_SHALLOW TRUE SYSTEM)
  FetchContent_MakeAvailable(${name})
  execute_process(
    COMMAND git rev-parse HEAD
    WORKING_DIRECTORY ${${name}_SOURCE_DIR}
    OUTPUT_VARIABLE checked_out
    OUTPUT_STRIP_TRAILING_WHITESPACE)
  if(NOT checked_out STREQUAL commit)
    message(FATAL_ERROR "${name} ${tag}: expected commit ${commit}, got '${checked_out}'")
  endif()
endfunction()

# simdjson: JSON parsing. Pinned 2026-11-23.
fetch_pinned(simdjson https://github.com/simdjson/simdjson.git v3.10.1
             e341c8b43861b43de29c48ab65f292d997096953)
if(ENGINE_SANITIZE)
  # simdjson's arm64 indexer writes positions in unrolled groups and computes clz(0) for the
  # unused slots, whose values are thrown away (write_index in
  # src/generic/stage1/json_structural_indexer.h). ARM's instruction defines clz(0) as 64, but
  # the C++ builtin leaves it undefined, so UBSan's "builtin" check stops the run there (seen on
  # the macOS runner). That one check is turned off for simdjson's own code; ours keeps it.
  target_compile_options(simdjson PRIVATE -fno-sanitize=builtin)
endif()

# GoogleTest: unit tests.
set(BUILD_GMOCK OFF CACHE BOOL "" FORCE)
set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
fetch_pinned(googletest https://github.com/google/googletest.git v1.15.2
             b514bdc898e2951020cbdca1304b75f5950d1f59)

# Google Benchmark: only for the release preset, which builds the benchmarks.
if(ENGINE_BENCHMARKS)
  set(BENCHMARK_ENABLE_TESTING OFF CACHE BOOL "" FORCE)
  set(BENCHMARK_ENABLE_GTEST_TESTS OFF CACHE BOOL "" FORCE)
  set(BENCHMARK_ENABLE_INSTALL OFF CACHE BOOL "" FORCE)
  set(BENCHMARK_INSTALL_DOCS OFF CACHE BOOL "" FORCE)
  set(BENCHMARK_ENABLE_WERROR OFF CACHE BOOL "" FORCE)
  # v1.9.1 is an annotated tag; this is the commit it points to.
  fetch_pinned(benchmark https://github.com/google/benchmark.git v1.9.1
               c58e6d0710581e3a08d65c349664128a8d9a2461)
endif()

# zlib reads the recorder's gzip files. It comes with macOS and every Linux system.
find_package(ZLIB REQUIRED)
