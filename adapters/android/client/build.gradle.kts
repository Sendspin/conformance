plugins {
    kotlin("jvm")
}

kotlin {
    jvmToolchain(21)
}

tasks.jar {
    archiveBaseName.set("conformance-client")
    archiveVersion.set("")
    manifest {
        attributes["Main-Class"] = "com.sendspin.conformance.android.MainKt"
    }
    exclude("META-INF/*.SF", "META-INF/*.DSA", "META-INF/*.RSA")
    from(provider { configurations.runtimeClasspath.get().files.map { if (it.isDirectory) it else zipTree(it) } })
    duplicatesStrategy = DuplicatesStrategy.EXCLUDE
}

dependencies {
    implementation("com.sendspindroid:shared")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.10.2")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.8.0")
}
