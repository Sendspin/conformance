pluginManagement {
    plugins {
        kotlin("jvm") version "2.3.0"
    }
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "android-conformance-client"

// SendspinDroid's protocol layer, built from the clone. Its settings include the
// Android modules too, so configuring this build needs an Android SDK.
includeBuild("../../../repos/SendspinDroid/android") {
    dependencySubstitution {
        substitute(module("com.sendspindroid:shared")).using(project(":shared"))
    }
}
