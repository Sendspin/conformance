cmake_minimum_required(VERSION 3.25)
project(conformance-sendspin-server-cpp-server C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(SENDSPIN_SERVER_CPP_REPO "${CMAKE_CURRENT_SOURCE_DIR}/../../../repos/sendspin-server-cpp"
    CACHE PATH "Path to sendspin-server-cpp repository checkout")

# The harness hands each case its endpoint through the registry file, so the library's mDNS
# backend is not built.
set(SENDSPIN_SERVER_ENABLE_DISCOVERY OFF CACHE BOOL "" FORCE)

# The library fetches Boost.Beast, ArduinoJson, noise-c, micro-flac and its other pinned
# dependencies via FetchContent at configure time.
add_subdirectory(${SENDSPIN_SERVER_CPP_REPO} sendspin-server-cpp EXCLUDE_FROM_ALL)

find_package(OpenSSL 3 REQUIRED)

add_executable(conformance-sendspin-server-cpp-server
    main.cpp
    fixture.cpp
    json_file.cpp
    scenarios.cpp
    server_session.cpp
    trace_recorder.cpp
)
target_link_libraries(conformance-sendspin-server-cpp-server PRIVATE
    sendspin_server_cpp
    ArduinoJson
    micro_flac
    OpenSSL::Crypto
)
