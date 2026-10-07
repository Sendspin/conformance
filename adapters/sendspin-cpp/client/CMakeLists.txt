cmake_minimum_required(VERSION 3.16)
project(conformance-sendspin-cpp-client CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(SENDSPIN_CPP_REPO "${CMAKE_CURRENT_SOURCE_DIR}/../../../repos/sendspin-cpp"
    CACHE PATH "Path to sendspin-cpp repository checkout")

# sendspin-cpp host build brings in IXWebSocket and ArduinoJson via FetchContent
add_subdirectory(${SENDSPIN_CPP_REPO} sendspin-cpp EXCLUDE_FROM_ALL)

find_package(OpenSSL REQUIRED)

add_executable(conformance-sendspin-cpp-client main.cpp)
target_link_libraries(conformance-sendspin-cpp-client PRIVATE sendspin)
target_link_libraries(conformance-sendspin-cpp-client PRIVATE OpenSSL::Crypto)
