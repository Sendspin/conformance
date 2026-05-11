pluginManagement {
    plugins {
        kotlin("jvm") version "2.0.0"
    }
    repositories {
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        mavenCentral()
    }
    versionCatalogs {
        create("libs") {
            from(files("../../../repos/sendspin-jvm/gradle/libs.versions.toml"))
        }
    }
}

rootProject.name = "sendspin-jvm-conformance-client"

include(":sendspin-protocol")
project(":sendspin-protocol").projectDir = file("../../../repos/sendspin-jvm/sendspin-protocol")
