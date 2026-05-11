plugins {
    kotlin("jvm")
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    jvmToolchain(17)
}

tasks.jar {
    archiveBaseName.set("conformance-client")
    archiveVersion.set("")
    manifest {
        attributes["Main-Class"] = "com.sendspin.conformance.MainKt"
    }
    from(provider { configurations.runtimeClasspath.get().files.map { if (it.isDirectory) it else zipTree(it) } })
    duplicatesStrategy = DuplicatesStrategy.EXCLUDE
}

dependencies {
    implementation(project(":sendspin-protocol"))
    implementation(libs.moshi.kotlin)
    implementation(libs.okhttp)
    implementation(libs.coroutines.core)
    implementation(libs.json)
}
