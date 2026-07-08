cmake_minimum_required(VERSION 3.25)
project(kalshi_collector CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(ixwebsocket CONFIG REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(nlohmann_json CONFIG REQUIRED)
find_package(doctest CONFIG REQUIRED)

add_library(collector_core
  src/tape.cpp
  src/auth.cpp
)
target_include_directories(collector_core PUBLIC src)
target_link_libraries(collector_core PUBLIC OpenSSL::Crypto)

add_executable(collector src/main.cpp)
target_link_libraries(collector PRIVATE
  collector_core
  ixwebsocket::ixwebsocket
  nlohmann_json::nlohmann_json
)
if(WIN32)
  # mbedtls (ixwebsocket's TLS backend) uses BCryptGenRandom but the vcpkg
  # target does not propagate the system library.
  target_link_libraries(collector PRIVATE bcrypt crypt32)
endif()

add_executable(collector_tests tests/test_core.cpp)
target_link_libraries(collector_tests PRIVATE
  collector_core
  doctest::doctest
  nlohmann_json::nlohmann_json
)

enable_testing()
add_test(NAME core COMMAND collector_tests)
