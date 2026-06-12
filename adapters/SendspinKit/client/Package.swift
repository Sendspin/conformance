// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "ConformanceSendspinKitClient",
    platforms: [
        .macOS(.v14),
    ],
    dependencies: [
        .package(path: "../../../repos/SendspinKit"),
    ],
    targets: [
        .executableTarget(
            name: "ConformanceSendspinKitClient",
            dependencies: [
                .product(name: "SendspinKit", package: "SendspinKit"),
            ]
        ),
    ]
)
