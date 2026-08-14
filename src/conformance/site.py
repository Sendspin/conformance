"""Static HTML report generation."""

from __future__ import annotations

import html
import json
import shutil
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .declared_formats import collect_declared_formats, declared_formats_for_implementation
from .environment import build_log_filename
from .implementations import IMPLEMENTATIONS, implementation_names, implementations_for_scenario
from .io import read_json, write_json
from .repository_versions import collect_repository_versions
from .scenarios import get_scenario, ordered_scenarios

HEAD_ASSETS = """
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            retro: {
              cream: "#f4e7d3",
              paper: "#fbf2e5",
              shell: "#efdcc7",
              line: "#c79e72",
              bark: "#4b2f1b",
              walnut: "#63391d",
              burnt: "#8a471e",
              copper: "#b66531",
              clay: "#d7a16d",
              code: "#2b1f17"
            }
          },
          fontFamily: {
            sans: ["Avenir Next", "Segoe UI", "system-ui", "sans-serif"],
            display: ["Iowan Old Style", "Palatino Linotype", "Book Antiqua", "serif"],
            mono: ["SFMono-Regular", "ui-monospace", "monospace"]
          }
        }
      }
    };
  </script>
  <style type="text/tailwindcss">
    @layer base {
      :root {
        --retro-cream: 244 231 211;
        --retro-paper: 251 242 229;
        --retro-shell: 239 220 199;
        --retro-line: 199 158 114;
        --retro-bark: 75 47 27;
        --retro-walnut: 99 57 29;
        --retro-burnt: 138 71 30;
        --retro-copper: 182 101 49;
        --retro-clay: 215 161 109;
        --retro-code: 43 31 23;
      }

      html {
        @apply scroll-smooth;
      }

      body {
        @apply min-h-screen font-sans antialiased;
        background-color: rgb(var(--retro-cream));
        color: rgb(var(--retro-bark));
      }

      h1, h2, h3, h4 {
        @apply font-display;
        color: rgb(var(--retro-bark));
      }

      a {
        @apply transition-colors duration-150;
      }

      pre {
        @apply font-mono text-[13px] leading-6;
      }

      ::selection {
        background-color: rgb(var(--retro-clay) / 0.45);
        color: rgb(var(--retro-bark));
      }
    }

    @layer components {
      .app-shell {
        background:
          linear-gradient(180deg, rgba(251, 242, 229, 0.7) 0%, rgba(244, 231, 211, 0.86) 100%),
          radial-gradient(circle at top left, rgba(182, 101, 49, 0.14), transparent 26%),
          radial-gradient(circle at top right, rgba(75, 47, 27, 0.08), transparent 28%);
      }

      .surface {
        @apply rounded-xl border backdrop-blur-sm;
        border-color: rgb(var(--retro-line) / 0.56);
        background-color: rgb(var(--retro-paper) / 0.9);
        box-shadow:
          0 1px 0 rgba(255, 255, 255, 0.55) inset,
          0 12px 30px rgba(92, 56, 29, 0.08);
      }

      .surface-muted {
        background-color: rgb(var(--retro-paper) / 0.74);
      }

      .surface-inset {
        @apply rounded-lg border;
        border-color: rgb(var(--retro-line) / 0.46);
        background-color: rgb(var(--retro-shell) / 0.52);
      }

      .page-header {
        @apply rounded-xl border px-5 py-5 sm:px-7 sm:py-6;
        border-color: rgb(var(--retro-line) / 0.62);
        background:
          linear-gradient(180deg, rgba(251, 242, 229, 0.96) 0%, rgba(247, 234, 217, 0.92) 100%);
        box-shadow:
          0 1px 0 rgba(255, 255, 255, 0.55) inset,
          0 22px 46px rgba(92, 56, 29, 0.10);
      }

      .page-header-overview {
        border-top: 4px solid rgb(var(--retro-copper));
      }

      .page-header-scenario {
        border-top: 4px solid rgb(var(--retro-burnt));
      }

      .page-header-case {
        border-top: 4px solid rgb(var(--retro-bark));
      }

      .eyebrow {
        @apply text-[11px] font-semibold uppercase tracking-[0.22em];
        color: rgb(var(--retro-burnt) / 0.78);
      }

      .subtle-copy {
        color: rgb(var(--retro-bark) / 0.68);
      }

      .muted-copy {
        color: rgb(var(--retro-bark) / 0.56);
      }

      .chip {
        @apply inline-flex items-center gap-2 rounded-md border px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.14em];
        border-color: rgb(var(--retro-line) / 0.58);
        background-color: rgb(var(--retro-paper) / 0.82);
        color: rgb(var(--retro-bark) / 0.78);
      }

      .chip:hover {
        background-color: rgb(var(--retro-paper));
        color: rgb(var(--retro-bark));
      }

      .resource-list {
        @apply mt-4 -mx-5;
        border-top: 1px solid rgb(var(--retro-line) / 0.28);
      }

      .resource-link {
        @apply flex items-center justify-between gap-3 border-b px-5 py-3 transition;
        border-color: rgb(var(--retro-line) / 0.28);
      }

      .resource-link:last-child {
        @apply border-b-0;
      }

      .resource-link:hover {
        background-color: rgb(var(--retro-paper) / 0.72);
      }

      .resource-link-label {
        @apply text-sm font-semibold;
        color: rgb(var(--retro-bark) / 0.88);
      }

      .resource-link-meta {
        @apply text-[11px] font-semibold uppercase tracking-[0.16em];
        color: rgb(var(--retro-burnt) / 0.7);
      }

      .resource-link-arrow {
        @apply text-sm;
        color: rgb(var(--retro-bark) / 0.42);
      }

      .breadcrumb {
        @apply flex flex-wrap items-center gap-x-2 gap-y-1 text-sm;
        color: rgb(var(--retro-bark) / 0.58);
      }

      .breadcrumb-link {
        color: rgb(var(--retro-burnt) / 0.86);
      }

      .breadcrumb-link:hover {
        color: rgb(var(--retro-bark));
      }

      .breadcrumb-current {
        color: rgb(var(--retro-bark));
      }

      .status-pill {
        @apply inline-flex items-center rounded-full border px-3 py-1.5 text-xs font-semibold uppercase tracking-[0.16em];
      }

      .status-passed {
        @apply border-amber-800/20 bg-amber-200/60;
        color: rgb(var(--retro-bark));
      }

      .status-failed {
        @apply border-orange-900/20 bg-orange-300/55;
        color: rgb(var(--retro-bark));
      }

      .status-skipped {
        @apply border-stone-500/20 bg-stone-200/70;
        color: rgb(var(--retro-bark));
      }

      .status-unsupported {
        @apply border-stone-500/20 bg-stone-300/70;
        color: rgb(var(--retro-bark));
      }

      .cell-link {
        @apply block rounded-md border px-3 py-3 text-left transition focus-visible:outline-none focus-visible:ring-2 sm:text-center;
      }

      .cell-link:focus-visible {
        --tw-ring-color: rgb(var(--retro-bark) / 0.45);
      }

      .cell-link:hover {
        transform: translateY(-1px);
      }

      .cell-passed {
        @apply border-amber-800/15 bg-amber-100/85 hover:bg-amber-100;
        color: rgb(var(--retro-bark));
      }

      .cell-failed {
        @apply border-orange-900/15 bg-orange-100/85 hover:bg-orange-100;
        color: rgb(var(--retro-bark));
      }

      .cell-skipped {
        @apply border-stone-500/15 bg-stone-100/85 hover:bg-stone-100;
        color: rgb(var(--retro-bark));
      }

      .cell-unsupported {
        @apply border-stone-500/15 bg-stone-100/90 hover:bg-stone-200/75;
        color: rgb(var(--retro-bark));
      }

      .nav-item {
        @apply block rounded-lg border px-4 py-4 transition;
        border-color: transparent;
      }

      .nav-item:hover {
        border-color: rgb(var(--retro-line) / 0.7);
        background-color: rgb(var(--retro-paper) / 0.86);
      }

      .nav-item-active {
        border-color: rgb(var(--retro-bark) / 0.28);
        background-color: rgb(var(--retro-bark));
        color: rgb(var(--retro-paper));
        box-shadow: 0 10px 24px rgba(75, 47, 27, 0.18);
      }

      .nav-item-active .nav-copy,
      .nav-item-active .nav-meta {
        color: rgb(var(--retro-paper) / 0.72);
      }

      .nav-item-compact {
        @apply px-3 py-3;
      }

      .nav-item-title {
        @apply text-sm font-semibold leading-snug;
      }

      .nav-meta-compact {
        @apply mt-2 flex items-center justify-between gap-3 text-[11px] font-semibold uppercase tracking-[0.16em];
        color: rgb(var(--retro-bark) / 0.56);
      }

      .nav-item-active .nav-meta-compact {
        color: rgb(var(--retro-paper) / 0.72);
      }

      .sidebar-tests {
        @apply hidden;
      }

      @media (min-width: 1024px) {
        .sidebar-tests {
          display: block;
        }
      }

      .sidebar-rail {
        @apply space-y-4 self-start;
      }

      @media (min-width: 1024px) {
        .sidebar-rail-lg {
          position: sticky;
          top: 1.5rem;
          max-height: calc(100vh - 3rem);
          overflow-y: auto;
          overscroll-behavior: contain;
          scrollbar-gutter: stable;
          padding-right: 0.35rem;
        }
      }

      @media (min-width: 1280px) {
        .sidebar-rail-xl {
          position: sticky;
          top: 1.5rem;
          max-height: calc(100vh - 3rem);
          overflow-y: auto;
          overscroll-behavior: contain;
          scrollbar-gutter: stable;
          padding-right: 0.35rem;
        }
      }

      .list-shell {
        @apply overflow-hidden rounded-xl border;
        border-color: rgb(var(--retro-line) / 0.56);
        background-color: rgb(var(--retro-paper) / 0.9);
        box-shadow: 0 10px 26px rgba(92, 56, 29, 0.08);
      }

      .list-row {
        @apply block border-b px-4 py-4 transition sm:px-5;
        border-color: rgb(var(--retro-line) / 0.34);
      }

      .list-row:last-child {
        border-bottom: 0;
      }

      .list-row:hover {
        background-color: rgb(var(--retro-paper));
      }

      .detail-card {
        @apply rounded-lg border px-4 py-4;
        border-color: rgb(var(--retro-line) / 0.46);
        background-color: rgb(var(--retro-paper) / 0.72);
      }

      .tab-strip {
        @apply flex flex-wrap items-end gap-2 border-b;
        border-color: rgb(var(--retro-line) / 0.4);
      }

      .keyval {
        @apply grid gap-3;
      }

      .keyval-row {
        @apply flex items-start justify-between gap-4 border-b pb-3;
        border-color: rgb(var(--retro-line) / 0.28);
      }

      .keyval-row:last-child {
        @apply border-b-0 pb-0;
      }

      .keyval-row-stack {
        @apply flex-col items-start justify-start gap-1.5;
      }

      .keyval-row-stack > :last-child {
        @apply block w-full break-words leading-5;
      }

      .tab-button {
        @apply relative rounded-t-lg border px-4 py-2.5 text-sm font-semibold transition;
        margin-bottom: -1px;
      }

      .tab-idle {
        border-color: rgb(var(--retro-line) / 0.5);
        background-color: rgb(var(--retro-shell) / 0.44);
        color: rgb(var(--retro-bark) / 0.7);
      }

      .tab-idle:hover {
        background-color: rgb(var(--retro-paper) / 0.92);
        color: rgb(var(--retro-bark));
      }

      .tab-active {
        border-color: rgb(var(--retro-line) / 0.62);
        border-bottom-color: rgb(var(--retro-paper) / 0.94);
        background-color: rgb(var(--retro-paper) / 0.94);
        color: rgb(var(--retro-bark));
        box-shadow:
          0 -1px 0 rgba(255, 255, 255, 0.48) inset,
          0 10px 18px rgba(75, 47, 27, 0.08);
      }

      .code-shell {
        @apply overflow-hidden rounded-lg border;
        border-color: rgb(var(--retro-line) / 0.46);
        background-color: rgb(var(--retro-paper) / 0.76);
      }

      .code-shell-header {
        @apply flex items-center justify-between gap-3 px-4 py-3 text-base sm:text-lg;
        border-bottom: 1px solid rgb(var(--retro-line) / 0.28);
      }

      .code-shell-raw {
        @apply text-[11px] font-semibold uppercase tracking-[0.16em];
        color: rgb(var(--retro-burnt) / 0.76);
      }

      .code-shell-raw:hover {
        color: rgb(var(--retro-bark));
      }

      .code-block {
        @apply max-h-[28rem] overflow-auto px-4 py-4;
        background-color: rgb(var(--retro-code));
        color: rgb(var(--retro-paper));
      }
    }
  </style>
"""

TABS_SCRIPT = """
document.querySelectorAll("[data-tabset]").forEach((tabset) => {
  const buttons = Array.from(tabset.querySelectorAll("[data-tab-button]"));
  const panels = Array.from(tabset.querySelectorAll("[data-tab-panel]"));
  const activate = (name) => {
    buttons.forEach((button) => {
      const isActive = button.dataset.tabButton === name;
      button.classList.toggle("tab-active", isActive);
      button.classList.toggle("tab-idle", !isActive);
      button.setAttribute("aria-selected", String(isActive));
    });
    panels.forEach((panel) => {
      panel.hidden = panel.dataset.tabPanel !== name;
    });
  };

  buttons.forEach((button) => {
    button.addEventListener("click", () => activate(button.dataset.tabButton));
  });

  const initial = buttons.find((button) => button.dataset.defaultTab === "true") ?? buttons[0];
  if (initial) {
    activate(initial.dataset.tabButton);
  }
});
"""

STATUS_LABELS = {
    "passed": "Passed",
    "failed": "Failed",
    "skipped": "Skipped",
    "unsupported": "Unsupported",
}

STATUS_CLASSES = {
    "passed": "status-passed",
    "failed": "status-failed",
    "skipped": "status-skipped",
    "unsupported": "status-unsupported",
}

CELL_CLASSES = {
    "passed": "cell-passed",
    "failed": "cell-failed",
    "skipped": "cell-skipped",
    "unsupported": "cell-unsupported",
}

CLAIM_PILL_EXERCISED = "status-pill border-amber-800/20 bg-amber-100/70 text-retro-bark"
# An untested claim is neither a pass nor a failure, so it deliberately avoids
# the filled pass/fail pills and reads as an open question instead.
CLAIM_PILL_UNVERIFIED = "status-pill border-dashed border-retro-line/60 bg-transparent text-retro-bark/60"

DECLARED_FORMAT_COLUMNS = ("Declared format", "Claim status", "Declared in", "Exercised by")

GITHUB_REPO_URL = "https://github.com/balloob-travel/conformance"
SENDSPIN_AUDIO_URL = "https://sendspin-audio.com/"
SCENARIOS_REPO_PATH = "src/conformance/scenarios.py"


def _data_dir(results_dir: Path) -> Path:
    data_dir = results_dir / "data"
    if not (data_dir / "index.json").exists():
        raise FileNotFoundError(f"Missing results index at {data_dir / 'index.json'}")
    return data_dir


def _build_results_index(data_dir: Path) -> dict[str, dict[str, Any]]:
    build_report = data_dir / "build-report.json"
    if not build_report.exists():
        return {}
    payload = read_json(build_report)
    results = payload.get("results")
    if not isinstance(results, list):
        return {}
    return {
        (
            str(result.get("environment_id") or "default"),
            str(result["adapter"]),
        ): result
        for result in results
        if isinstance(result, dict) and "adapter" in result
    }


def _repository_versions(data_dir: Path, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _normalized_repository(repository: dict[str, Any]) -> dict[str, Any] | None:
        key = str(repository.get("key") or "")
        if key == "conformance":
            return None
        normalized = dict(repository)
        implementation = IMPLEMENTATIONS.get(key)
        if implementation is None:
            return normalized
        repo_url = f"https://github.com/Sendspin/{implementation.repo_dirname}"
        commit_sha = normalized.get("commit_sha")
        latest_release_tag = normalized.get("latest_release_tag")
        normalized["remote_url"] = repo_url
        normalized["commit_url"] = (
            None
            if not isinstance(commit_sha, str) or not commit_sha
            else f"{repo_url}/commit/{quote(commit_sha, safe='')}"
        )
        normalized["release_url"] = (
            None
            if not isinstance(latest_release_tag, str) or not latest_release_tag
            else f"{repo_url}/releases/tag/{quote(latest_release_tag, safe='')}"
        )
        normalized["compare_url"] = (
            None
            if not isinstance(latest_release_tag, str)
            or not latest_release_tag
            or not isinstance(commit_sha, str)
            or not commit_sha
            else f"{repo_url}/compare/{quote(latest_release_tag, safe='')}...{quote(commit_sha, safe='')}"
        )
        return normalized

    repositories_path = data_dir / "repositories.json"
    if repositories_path.exists():
        payload = read_json(repositories_path)
        repositories = payload.get("repositories")
        if isinstance(repositories, list):
            normalized = [
                _normalized_repository(repository)
                for repository in repositories
                if isinstance(repository, dict)
            ]
            return [repository for repository in normalized if repository is not None]
    return collect_repository_versions(results)


def _sync_case_artifacts(data_dir: Path, site_dir: Path) -> str:
    target_dir = site_dir / "data"
    if target_dir.resolve() == data_dir.resolve():
        return "data"
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(data_dir, target_dir)
    return "data"


def _prepare_output_dir(site_dir: Path, dirname: str) -> Path:
    output_dir = site_dir / dirname
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _clear_legacy_dirs(site_dir: Path) -> None:
    legacy_tests = site_dir / "tests"
    if legacy_tests.exists():
        shutil.rmtree(legacy_tests)


def _scenario_results(results: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[str(result["scenario_id"])].append(result)

    ordered_ids = [scenario.id for scenario in ordered_scenarios() if scenario.id in grouped]
    ordered_ids.extend(
        sorted(scenario_id for scenario_id in grouped if get_scenario(scenario_id) is None)
    )
    return [(scenario_id, grouped[scenario_id]) for scenario_id in ordered_ids]


def _result_environment(result: dict[str, Any]) -> tuple[str, str]:
    environment_id = str(result.get("environment_id") or "default")
    environment_name = str(result.get("environment_name") or environment_id)
    return environment_id, environment_name


def _environment_sort_key(environment_id: str, environment_name: str) -> tuple[int, str, str]:
    preferred_order = {
        "linux": 0,
        "macos": 1,
        "windows": 2,
    }
    return (
        preferred_order.get(environment_id, 99),
        environment_name.lower(),
        environment_id,
    )


def _environment_results(
    results: list[dict[str, Any]],
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    names: dict[str, str] = {}
    for result in results:
        environment_id, environment_name = _result_environment(result)
        grouped[environment_id].append(result)
        names.setdefault(environment_id, environment_name)
    ordered_ids = sorted(
        grouped,
        key=lambda environment_id: _environment_sort_key(
            environment_id,
            names[environment_id],
        ),
    )
    return [
        (environment_id, names[environment_id], grouped[environment_id])
        for environment_id in ordered_ids
    ]


def _scenario_name(scenario_id: str) -> str:
    scenario = get_scenario(scenario_id)
    return scenario.display_name if scenario is not None else scenario_id


def _scenario_description(scenario_id: str) -> str:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        return "No scenario description is registered for this test."
    return scenario.description


def _scenario_intro(scenario_id: str) -> str:
    description = _scenario_description(scenario_id).strip()
    first_sentence = description.split(". ")[0].strip()
    if first_sentence.endswith("."):
        return first_sentence
    return f"{first_sentence}."


def _scenario_href(scenario_id: str) -> str:
    return f"scenarios/{scenario_id}.html"


def _implementation_href(implementation: str) -> str:
    return f"implementations/{implementation}.html"


def _external_chip(label: str, href: str) -> str:
    return (
        f"<a class='chip' href='{html.escape(href, quote=True)}' "
        "target='_blank' rel='noreferrer'>"
        f"{html.escape(label)}"
        "</a>"
    )


def _resource_link(label: str, href: str, *, meta: str = "External") -> str:
    return (
        f"<a class='resource-link' href='{html.escape(href, quote=True)}' "
        "target='_blank' rel='noreferrer'>"
        "<span class='min-w-0'>"
        f"<span class='resource-link-label block'>{html.escape(label)}</span>"
        f"<span class='resource-link-meta mt-1 block'>{html.escape(meta)}</span>"
        "</span>"
        "<span aria-hidden='true' class='resource-link-arrow'>&nearr;</span>"
        "</a>"
    )


def _resource_section(*links: str) -> str:
    if not links:
        return ""
    return (
        "<section class='surface p-5'>"
        "<p class='eyebrow'>Resources</p>"
        f"<div class='resource-list'>{''.join(links)}</div>"
        "</section>"
    )


def _sidebar_resources(*, scenario_id: str | None = None) -> str:
    links = [
        _resource_link("Conformance source", GITHUB_REPO_URL, meta="GitHub"),
        _resource_link("Sendspin-audio.com", SENDSPIN_AUDIO_URL, meta="Website"),
    ]
    if scenario_id is not None:
        links.append(
            _resource_link(
                "Test source",
                _scenario_source_url(scenario_id),
                meta="Scenario source",
            )
        )
    return _resource_section(*links)


def _sidebar_nav(*items: tuple[str, str, str]) -> str:
    links = "".join(
        (
            f"<a class='nav-item' href='{html.escape(href, quote=True)}'>"
            f"<p class='text-sm font-semibold'>{html.escape(title)}</p>"
            f"<p class='nav-copy mt-1 text-sm subtle-copy'>{html.escape(description)}</p>"
            "</a>"
        )
        for title, href, description in items
    )
    return "<section class='surface p-5'><div class='space-y-2'>" + links + "</div></section>"


def _github_blob_url(repo_path: str, *, line: int | None = None) -> str:
    url = f"{GITHUB_REPO_URL}/blob/main/{repo_path}"
    if line is not None:
        return f"{url}#L{line}"
    return url


@lru_cache(maxsize=None)
def _scenario_source_line(scenario_id: str) -> int | None:
    source_path = Path(__file__).resolve().with_name("scenarios.py")
    needle = f'id="{scenario_id}"'
    for line_number, line in enumerate(
        source_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if needle in line:
            return line_number
    return None


def _scenario_source_url(scenario_id: str) -> str:
    return _github_blob_url(
        SCENARIOS_REPO_PATH,
        line=_scenario_source_line(scenario_id),
    )


def _case_slug(result: dict[str, Any]) -> str:
    return Path(str(result["case_dir"])).name


def _case_href(result: dict[str, Any]) -> str:
    return f"cases/{_case_slug(result)}.html"


def _case_key(result: dict[str, Any]) -> tuple[str, str]:
    return (str(result["server_impl"]), str(result["client_impl"]))


def _implementation_label(name: str) -> str:
    implementation = IMPLEMENTATIONS.get(name)
    return implementation.display_name if implementation is not None else name


def _implementation_repo_url(name: str) -> str | None:
    implementation = IMPLEMENTATIONS.get(name)
    if implementation is None:
        return None
    return f"https://github.com/Sendspin/{implementation.repo_dirname}"


def _implementation_subtitle(name: str) -> str | None:
    label = _implementation_label(name)
    return None if label == name else name


def _implementation_identity(
    name: str,
    *,
    role_label: str | None = None,
    primary_class: str,
    secondary_class: str,
) -> str:
    parts: list[str] = ["<div class='space-y-1'>"]
    if role_label is not None:
        parts.append(
            "<p class='text-[11px] font-semibold uppercase tracking-[0.18em] text-retro-bark/45'>"
            f"{html.escape(role_label)}"
            "</p>"
        )
    parts.append(
        f"<p class='{html.escape(primary_class, quote=True)}'>{html.escape(_implementation_label(name))}</p>"
    )
    subtitle = _implementation_subtitle(name)
    if subtitle is not None:
        parts.append(
            f"<p class='{html.escape(secondary_class, quote=True)}'>{html.escape(subtitle)}</p>"
        )
    parts.append("</div>")
    return "".join(parts)


def _ordered_implementation_subset(names: set[str]) -> list[str]:
    ordered = [name for name in implementation_names() if name in names]
    ordered.extend(sorted(name for name in names if name not in IMPLEMENTATIONS))
    return ordered


def _implementation_results(
    results: list[dict[str, Any]],
    implementation: str,
) -> list[dict[str, Any]]:
    return [
        result
        for result in results
        if str(result["server_impl"]) == implementation
        or str(result["client_impl"]) == implementation
    ]


def _repository_release_delta(repository: dict[str, Any]) -> str:
    latest_release_tag = repository.get("latest_release_tag")
    commits_ahead = repository.get("commits_ahead_of_release")
    if isinstance(latest_release_tag, str) and latest_release_tag:
        if isinstance(commits_ahead, int):
            suffix = "commit" if commits_ahead == 1 else "commits"
            return f"{latest_release_tag} · {commits_ahead} {suffix} ahead"
        return latest_release_tag
    return "No release tag found"


def _repository_link(label: str, href: str | None, *, mono: bool = False) -> str:
    classes = "text-sm font-semibold text-retro-burnt hover:text-retro-bark"
    if mono:
        classes += " font-mono"
    if href is None:
        return f"<span class='{classes}'>{_escape(label)}</span>"
    return (
        f"<a class='{classes}' href='{html.escape(href, quote=True)}' "
        "target='_blank' rel='noreferrer'>"
        f"{_escape(label)}"
        "</a>"
    )


def _repository_versions_section(
    repositories: list[dict[str, Any]],
    *,
    filtered_implementations: set[str],
) -> str:
    if not repositories:
        return ""

    rows: list[str] = []
    for repository in repositories:
        display_name = str(repository.get("display_name") or repository.get("key") or "Repository")
        available = bool(repository.get("available"))
        commit_label = str(repository.get("commit_short_sha") or "Unavailable")
        commit_url = repository.get("commit_url")
        commit_subject = str(repository.get("commit_subject") or "Git metadata was unavailable for this checkout.")
        committed_at = str(repository.get("committed_at") or "")
        latest_change = _repository_link(commit_subject, commit_url if available else None)
        commit_markup = _repository_link(commit_label, commit_url if available else None, mono=True)
        release_text = _repository_release_delta(repository)
        compare_url = repository.get("compare_url") or repository.get("release_url")
        release_markup = _repository_link(release_text, str(compare_url) if compare_url else None)
        repo_url = repository.get("remote_url")
        implementation_key = str(repository.get("key") or "")
        title_markup = _repository_link(display_name, str(repo_url) if repo_url else None)
        subtitle_parts: list[str] = []
        client_code_url = repository.get("client_code_url")
        if implementation_key in filtered_implementations:
            subtitle_parts.append(
                f"<a class='text-xs font-semibold uppercase tracking-[0.16em] text-retro-burnt hover:text-retro-bark' "
                f"href='{html.escape(_implementation_href(implementation_key), quote=True)}'>Filtered results</a>"
            )
        if client_code_url:
            client_code_markup = (
                f"<a class='text-xs font-semibold uppercase tracking-[0.16em] text-retro-burnt hover:text-retro-bark' "
                f"href='{html.escape(str(client_code_url), quote=True)}' target='_blank' rel='noreferrer'>Client code</a>"
            )
        else:
            client_code_markup = ""
        client_code_row = f"<div class='mt-2'>{client_code_markup}</div>" if client_code_markup else ""

        rows.append(
            "<div class='py-4 sm:py-5'>"
            "<div class='grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,0.85fr)_minmax(0,1.7fr)_minmax(0,1fr)] xl:items-start'>"
            "<div class='min-w-0'>"
            f"<div class='text-sm font-semibold'>{title_markup}</div>"
            f"<div class='mt-2 flex flex-wrap items-center gap-2'>{''.join(subtitle_parts)}</div>"
            f"{client_code_row}"
            "</div>"
            "<div class='min-w-0'>"
            "<p class='eyebrow'>Commit</p>"
            f"<div class='mt-2'>{commit_markup}</div>"
            "</div>"
            "<div class='min-w-0'>"
            "<p class='eyebrow'>Latest change</p>"
            f"<div class='mt-2 break-words'>{latest_change}</div>"
            f"<p class='mt-1 text-xs muted-copy'>{_escape(committed_at) if committed_at else 'Date unavailable'}</p>"
            "</div>"
            "<div class='min-w-0'>"
            "<p class='eyebrow'>Release delta</p>"
            f"<div class='mt-2 break-words'>{release_markup}</div>"
            "</div>"
            "</div>"
            "</div>"
        )

    return (
        "<section class='surface p-5 sm:p-6'>"
        "<div class='max-w-3xl'>"
        "<p class='eyebrow'>Repository versions</p>"
        "<h2 class='mt-2 text-2xl sm:text-3xl'>Checked-out revisions</h2>"
        "<p class='mt-3 text-sm leading-6 subtle-copy sm:text-base'>"
        "These are the exact checked-out commits used for this report. Each row links to the selected "
        "GitHub commit, shows the latest change included in that checkout, and notes how far the checkout "
        "sits ahead of the latest tagged release."
        "</p>"
        "</div>"
        "<div class='mt-5 divide-y' style='border-color: rgb(var(--retro-line) / 0.28)'>"
        f"{''.join(rows)}"
        "</div>"
        "</section>"
    )


def _display_status(result: dict[str, Any]) -> str:
    status = str(result["status"])
    if status != "failed":
        return status

    reason = str(result.get("reason") or "").lower()
    unsupported_markers = (
        "does not support",
        "currently a client library",
        "does not yet expose",
        "not a server implementation",
        "only supports client-initiated",
        "only supports server-initiated",
    )
    if any(marker in reason for marker in unsupported_markers):
        return "unsupported"
    return status


def _status_counts(results: list[dict[str, Any]]) -> Counter[str]:
    return Counter(_display_status(result) for result in results)


def _status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status.capitalize())


def _status_classes(status: str) -> str:
    return STATUS_CLASSES.get(status, "status-skipped")


def _cell_classes(status: str) -> str:
    return CELL_CLASSES.get(status, "cell-skipped")


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _escape(value: Any) -> str:
    return html.escape(str(value))


def _safe_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _read_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _read_pretty_json(path: Path) -> str | None:
    if not path.exists():
        return None
    return _safe_json(read_json(path))


def _display_result_json(path: Path, fallback: dict[str, Any]) -> str:
    payload = dict(fallback)
    if path.exists():
        payload = read_json(path)
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.pop("environment_id", None)
        payload.pop("environment_name", None)
    return _safe_json(payload)


def _summary_cards(
    *,
    counts: Counter[str],
    total_label: str,
    total_value: int,
) -> str:
    items = [("Total", total_value, total_label), ("Passed", counts.get("passed", 0), "passing")]
    if counts.get("unsupported", 0):
        items.append(("Unsupported", counts["unsupported"], "not supported"))
    if counts.get("failed", 0) or not counts.get("unsupported", 0):
        items.append(("Failed", counts.get("failed", 0), "failing"))
    if counts.get("skipped", 0):
        items.append(("Skipped", counts["skipped"], "skipped"))
    cards = "".join(
        (
            "<div class='detail-card'>"
            f"<p class='eyebrow'>{_escape(label)}</p>"
            f"<p class='mt-2 text-2xl font-semibold'>{_escape(value)}</p>"
            f"<p class='mt-1 text-sm muted-copy'>{_escape(subtitle)}</p>"
            "</div>"
        )
        for label, value, subtitle in items
    )
    return f"<div class='grid gap-3 sm:grid-cols-2 xl:grid-cols-5'>{cards}</div>"


def _status_pill_row(counts: Counter[str]) -> str:
    pills = [
        f"<span class='status-pill {_status_classes('passed')}'>{counts.get('passed', 0)} passed</span>"
    ]
    if counts.get("unsupported", 0):
        pills.append(
            f"<span class='status-pill {_status_classes('unsupported')}'>{counts.get('unsupported', 0)} unsupported</span>"
        )
    if counts.get("failed", 0):
        pills.append(
            f"<span class='status-pill {_status_classes('failed')}'>{counts.get('failed', 0)} failed</span>"
        )
    if counts.get("skipped", 0):
        pills.append(
            f"<span class='status-pill {_status_classes('skipped')}'>{counts.get('skipped', 0)} skipped</span>"
        )
    return "".join(pills)


def _breadcrumb(items: list[tuple[str, str | None]]) -> str:
    parts: list[str] = []
    for index, (label, href) in enumerate(items):
        if index:
            parts.append("<span aria-hidden='true' class='muted-copy'>&rarr;</span>")
        if href is None:
            parts.append(f"<span class='breadcrumb-current'>{_escape(label)}</span>")
            continue
        parts.append(
            f"<a class='breadcrumb-link' href='{html.escape(href, quote=True)}'>{_escape(label)}</a>"
        )
    return f"<nav class='breadcrumb' aria-label='Breadcrumb'>{''.join(parts)}</nav>"


def _page_header(
    *,
    accent: str,
    breadcrumb: str,
    kicker: str,
    title: str,
    description: str,
    actions: str = "",
    meta: str = "",
) -> str:
    actions_markup = (
        "<div class='flex flex-wrap gap-2 xl:max-w-sm xl:justify-end'>"
        f"{actions}"
        "</div>"
        if actions
        else ""
    )
    meta_markup = f"<div class='mt-5'>{meta}</div>" if meta else ""
    return (
        f"<section class='page-header page-header-{html.escape(accent, quote=True)}'>"
        f"{breadcrumb}"
        "<div class='mt-4 flex flex-col gap-5 xl:flex-row xl:items-start xl:justify-between'>"
        "<div class='min-w-0 max-w-4xl'>"
        f"<p class='eyebrow'>{_escape(kicker)}</p>"
        f"<h1 class='mt-2 text-3xl leading-tight sm:text-4xl'>{_escape(title)}</h1>"
        f"<p class='mt-3 max-w-3xl text-sm leading-6 subtle-copy sm:text-base'>{_escape(description)}</p>"
        f"{meta_markup}"
        "</div>"
        f"{actions_markup}"
        "</div>"
        "</section>"
    )


def _page_shell(
    *,
    title: str,
    body: str,
    include_tabs: bool = False,
) -> str:
    tabs_script = f"<script>{TABS_SCRIPT}</script>" if include_tabs else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>{html.escape(title)}</title>
{HEAD_ASSETS}
</head>
<body>
  {body}
  {tabs_script}
</body>
</html>
"""


def _nav_scenarios(
    scenario_groups: list[tuple[str, list[dict[str, Any]]]],
    *,
    current_scenario_id: str | None = None,
    href_prefix: str = "",
) -> str:
    items: list[str] = []
    for scenario_id, scenario_results in scenario_groups:
        counts = _status_counts(scenario_results)
        active_class = " nav-item-active" if scenario_id == current_scenario_id else ""
        items.append(
            f"<a class='nav-item nav-item-compact{active_class}' href='{html.escape(href_prefix + _scenario_href(scenario_id), quote=True)}'>"
            "<div class='min-w-0'>"
            f"<p class='nav-item-title'>{html.escape(_scenario_name(scenario_id))}</p>"
            "</div>"
            "<div class='nav-meta nav-meta-compact'>"
            f"<span>{counts.get('passed', 0)}/{len(scenario_results)} green</span>"
            f"<span class='normal-case tracking-normal'>{len(scenario_results)} pairings</span>"
            "</div>"
            "</a>"
        )
    return (
        "<section class='surface p-4 sm:p-5 sidebar-tests'>"
        "<p class='eyebrow'>Tests</p>"
        "<div class='mt-3 space-y-2'>"
        f"{''.join(items)}"
        "</div>"
        "</section>"
    )


def _render_matrix(
    results: list[dict[str, Any]],
    *,
    caption: str,
    href_builder: Callable[[dict[str, Any]], str],
    current_case_slug: str | None = None,
    server_implementations: list[str] | None = None,
    client_implementations: list[str] | None = None,
) -> str:
    if server_implementations is None or client_implementations is None:
        computed_server_implementations, computed_client_implementations = _matrix_axes(results)
        if server_implementations is None:
            server_implementations = computed_server_implementations
        if client_implementations is None:
            client_implementations = computed_client_implementations
    result_map = {
        (str(result["server_impl"]), str(result["client_impl"])): result for result in results
    }

    rows: list[str] = []
    for server_impl in server_implementations:
        cells = [
            (
                "<th scope='row' class='sticky left-0 z-10 min-w-[190px] border-b px-4 py-4 text-left' "
                "style='border-color: rgb(var(--retro-line) / 0.36); background-color: rgb(var(--retro-paper) / 0.97); vertical-align: middle'>"
                f"{_implementation_identity(server_impl, role_label='Server', primary_class='text-sm font-semibold text-retro-bark', secondary_class='text-xs text-retro-bark/54')}"
                "</th>"
            )
        ]
        for client_impl in client_implementations:
            result = result_map.get((server_impl, client_impl))
            if result is None:
                cells.append(
                    "<td class='border-b px-2 py-2' style='border-color: rgb(var(--retro-line) / 0.28); background-color: rgb(var(--retro-paper) / 0.4); vertical-align: middle'>"
                    "<div class='rounded-lg border border-dashed px-3 py-3 text-center text-xs uppercase tracking-[0.18em]' "
                    "style='border-color: rgb(var(--retro-line) / 0.35); color: rgb(var(--retro-bark) / 0.35)'>"
                    "No case"
                    "</div>"
                    "</td>"
                )
                continue

            status = _display_status(result)
            case_slug = _case_slug(result)
            current_class = " ring-2 ring-retro-bark/70" if case_slug == current_case_slug else ""
            title = (
                f"Server: {_implementation_label(server_impl)} | "
                f"Client: {_implementation_label(client_impl)} | "
                f"Status: {_status_label(status)}"
            )
            cells.append(
                "<td class='border-b px-2 py-2' style='border-color: rgb(var(--retro-line) / 0.28); background-color: rgb(var(--retro-paper) / 0.4); vertical-align: middle'>"
                f"<a class='cell-link {_cell_classes(status)}{current_class}' href='{html.escape(href_builder(result), quote=True)}' title='{html.escape(title, quote=True)}' aria-label='{html.escape(title, quote=True)}'>"
                f"<span class='block text-sm font-semibold'>{html.escape(_status_label(status))}</span>"
                "</a>"
                "</td>"
            )
        rows.append(f"<tr>{''.join(cells)}</tr>")

    headers = "".join(
        (
            "<th scope='col' class='min-w-[140px] border-b px-3 py-4 text-left align-bottom sm:text-center' "
            "style='border-color: rgb(var(--retro-line) / 0.36); background-color: rgb(var(--retro-shell) / 0.88)'>"
            f"{_implementation_identity(client_impl, role_label='Client', primary_class='text-sm font-semibold text-retro-bark', secondary_class='text-xs text-retro-bark/54')}"
            "</th>"
        )
        for client_impl in client_implementations
    )

    return (
        "<section class='list-shell overflow-hidden'>"
        "<div class='border-b px-4 py-4' "
        "style='border-color: rgb(var(--retro-line) / 0.42)'>"
        "<div>"
        "<p class='eyebrow'>Matrix</p>"
        f"<p class='mt-1 text-sm subtle-copy'>{html.escape(caption)}</p>"
        "</div>"
        "</div>"
        "<div class='overflow-x-auto'>"
        "<table class='min-w-[980px] w-full border-separate border-spacing-0 text-sm'>"
        "<thead>"
        "<tr>"
        "<th scope='col' class='sticky left-0 z-20 min-w-[190px] border-b px-4 py-4 text-left' "
        "style='border-color: rgb(var(--retro-line) / 0.36); background-color: rgb(var(--retro-shell))'>"
        "<p class='eyebrow'>Axis</p>"
        "<p class='mt-1 text-sm font-semibold text-retro-bark'>Server \\ Client</p>"
        "</th>"
        f"{headers}"
        "</tr>"
        "</thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
        "</div>"
        "</section>"
    )


def _matrix_axes(results: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    if not results:
        return implementation_names(), implementation_names()

    scenario_id = str(results[0]["scenario_id"])
    scenario = get_scenario(scenario_id)
    known_names = implementation_names()
    client_impls = list(known_names)
    client_impls.extend(
        sorted(
            {
                str(result["client_impl"])
                for result in results
                if str(result["client_impl"]) not in IMPLEMENTATIONS
            }
        )
    )

    if scenario is None:
        server_impls = sorted({str(result["server_impl"]) for result in results})
        return server_impls, client_impls
    server_impls = implementations_for_scenario(
        role="server",
        scenario=scenario,
        names=known_names,
    )
    extra_servers = sorted(
        {
            str(result["server_impl"])
            for result in results
            if str(result["server_impl"]) not in IMPLEMENTATIONS
        }
    )
    server_impls.extend(extra_servers)
    return server_impls, client_impls


def _render_code_panel(
    *,
    heading: str,
    content: str | None,
    mode: str,
    raw_href: str | None = None,
) -> str:
    body = content if content is not None else "No artifact was written for this section."
    whitespace = "whitespace-pre" if mode == "json" else "whitespace-pre-wrap"
    raw_link = (
        f"<a class='code-shell-raw' href='{html.escape(raw_href, quote=True)}'>RAW</a>"
        if raw_href is not None
        else ""
    )
    return (
        "<section class='code-shell'>"
        "<div class='code-shell-header'>"
        f"<h3>{html.escape(heading)}</h3>"
        f"{raw_link}"
        "</div>"
        f"<pre class='code-block {whitespace}'>{html.escape(body)}</pre>"
        "</section>"
    )


def _case_payload(result: dict[str, Any], *, data_dir: Path) -> dict[str, Any]:
    case_name = _case_slug(result)
    case_dir = data_dir / case_name
    return {
        "case_name": case_name,
        "case_dir": case_dir,
        "result_json": _display_result_json(case_dir / "result.json", result),
        "server_summary_json": _read_pretty_json(case_dir / "server-summary.json"),
        "client_summary_json": _read_pretty_json(case_dir / "client-summary.json"),
        "server_log": _read_text(case_dir / "server.log"),
        "client_log": _read_text(case_dir / "client.log"),
    }


def _build_panel_heading(
    *,
    label: str,
    adapter: str,
    build_result: dict[str, Any] | None,
) -> str:
    if build_result is None:
        return f"{label} build log · {adapter}"
    status = str(build_result.get("status") or "unknown")
    duration = build_result.get("duration_seconds")
    duration_suffix = (
        f" · {float(duration):.2f}s"
        if isinstance(duration, (int, float))
        else ""
    )
    return f"{label} build log · {adapter} · {status}{duration_suffix}"


def _build_panel_content(build_result: dict[str, Any] | None, *, adapter: str) -> str:
    if build_result is None:
        return f"No build report entry was written for {adapter}."
    detail = str(build_result.get("detail") or "").strip()
    return detail or "No build detail was recorded."


def _build_panels_for_case(
    *,
    data_dir: Path,
    environment_id: str,
    server_impl: str,
    client_impl: str,
    build_index: dict[tuple[str, str], dict[str, Any]],
) -> str:
    def _role_build(implementation: str, role: str) -> tuple[str | None, dict[str, Any] | None]:
        spec = IMPLEMENTATIONS.get(implementation)
        if spec is None:
            return None, None
        role_spec = spec.server if role == "server" else spec.client
        adapter = role_spec.build_adapter
        if adapter is None:
            return None, None
        return adapter, build_index.get((environment_id, adapter))

    server_adapter, server_build = _role_build(server_impl, "server")
    client_adapter, client_build = _role_build(client_impl, "client")

    if server_adapter is None and client_adapter is None:
        return ""

    if server_adapter is not None and server_adapter == client_adapter:
        shared_log = build_log_filename(environment_id, server_adapter)
        raw_href = f"../data/builds/{shared_log}"
        if not (data_dir / "builds" / shared_log).exists():
            raw_href = None
        return _render_code_panel(
            heading=_build_panel_heading(
                label="Shared",
                adapter=server_adapter,
                build_result=server_build or client_build,
            ),
            content=_build_panel_content(server_build or client_build, adapter=server_adapter),
            mode="log",
            raw_href=raw_href,
        )

    panels: list[str] = []
    if server_adapter is not None:
        server_log = build_log_filename(environment_id, server_adapter)
        server_raw = f"../data/builds/{server_log}"
        if not (data_dir / "builds" / server_log).exists():
            server_raw = None
        panels.append(
            _render_code_panel(
                heading=_build_panel_heading(
                    label="Server",
                    adapter=server_adapter,
                    build_result=server_build,
                ),
                content=_build_panel_content(server_build, adapter=server_adapter),
                mode="log",
                raw_href=server_raw,
            )
        )
    if client_adapter is not None:
        client_log = build_log_filename(environment_id, client_adapter)
        client_raw = f"../data/builds/{client_log}"
        if not (data_dir / "builds" / client_log).exists():
            client_raw = None
        panels.append(
            _render_code_panel(
                heading=_build_panel_heading(
                    label="Client",
                    adapter=client_adapter,
                    build_result=client_build,
                ),
                content=_build_panel_content(client_build, adapter=client_adapter),
                mode="log",
                raw_href=client_raw,
            )
        )
    return "<div class='grid gap-4 2xl:grid-cols-2'>" + "".join(panels) + "</div>"


def _render_index_page(results: list[dict[str, Any]], *, data_dir: Path) -> str:
    scenario_groups = _scenario_results(results)
    repositories = _repository_versions(data_dir, results)
    counts = _status_counts(results)
    filtered_implementations = {
        implementation
        for implementation in IMPLEMENTATIONS
        if _implementation_results(results, implementation)
    }
    overview_header = _page_header(
        accent="overview",
        breadcrumb=_breadcrumb([("Overview", None)]),
        kicker="Overview",
        title="Sendspin conformance overview",
        description=(
            "Sendspin is a local-network protocol for discovering peers and exchanging synchronized "
            "audio plus companion data such as metadata, artwork, and controller messages between "
            "servers and clients. This report tests how different Sendspin implementations interoperate "
            "with one another, with each matrix showing which server "
            "and client pairings pass the current conformance scenarios."
        ),
        actions=(
            f"{_external_chip('Conformance source', GITHUB_REPO_URL)}"
            f"{_external_chip('Sendspin-audio.com', SENDSPIN_AUDIO_URL)}"
        ),
        meta=_summary_cards(counts=counts, total_label="all scenarios", total_value=len(results)),
    )
    sections: list[str] = []
    for scenario_id, scenario_results in scenario_groups:
        scenario_counts = _status_counts(scenario_results)
        sections.append(
            "<section class='surface p-5 sm:p-6'>"
            "<div class='flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between'>"
            "<div class='max-w-3xl'>"
            "<p class='eyebrow'>Test</p>"
            f"<h2 class='mt-2 text-2xl sm:text-3xl'>{html.escape(_scenario_name(scenario_id))}</h2>"
            f"<p class='mt-3 text-sm leading-6 subtle-copy sm:text-base'>{html.escape(_scenario_description(scenario_id))}</p>"
            "</div>"
            "<div class='flex flex-wrap items-center gap-2 xl:justify-end'>"
            f"{_status_pill_row(scenario_counts)}"
            f"{_external_chip('View test source', _scenario_source_url(scenario_id))}"
            f"<a class='chip' href='{html.escape(_scenario_href(scenario_id), quote=True)}'>Scenario page</a>"
            "</div>"
            "</div>"
            "<div class='mt-5 space-y-4'>"
            f"{_render_matrix(scenario_results, caption='Select a server and client pairing to inspect.', href_builder=_case_href)}"
            "</div>"
            "</section>"
        )

    sections_html = ''.join(sections) if sections else '<section class="surface p-6 text-sm subtle-copy">No scenario results were found.</section>'
    body = (
        "<div class='app-shell'>"
        "<div class='mx-auto max-w-[1540px] px-4 py-4 sm:px-6 lg:px-8 lg:py-6'>"
        "<main class='space-y-6'>"
        f"{overview_header}"
        f"{sections_html}"
        f"{_repository_versions_section(repositories, filtered_implementations=filtered_implementations)}"
        "</main>"
        "</div>"
        "</div>"
    )
    return _page_shell(title="Sendspin Conformance", body=body)


def _case_chips(references: list[dict[str, Any]], *, qualify: bool) -> str:
    """Render case links. ``qualify`` marks weak or non-passing observations."""
    if not references:
        return "<span class='text-sm text-retro-bark/45'>&mdash;</span>"
    show_server = len({str(reference.get("server_impl")) for reference in references}) > 1
    chips: list[str] = []
    for reference in references:
        scenario_label = _scenario_name(str(reference.get("scenario_id") or ""))
        server_label = _implementation_label(str(reference.get("server_impl") or ""))
        status = str(reference.get("status") or "")
        sources = [str(source) for source in _str_list(reference.get("sources"))]
        chip_label = f"{scenario_label} · {server_label}" if show_server else scenario_label
        qualifiers: list[str] = []
        if qualify:
            # A format carried only by a failing case is still negotiated, and a
            # format only the client reported seeing is only the client's word
            # for it. Either way, calling it coverage unqualified overstates it.
            if status and status != "passed":
                qualifiers.append(status)
            if sources and not any(source.startswith("server.") for source in sources):
                qualifiers.append("client-reported")
        if qualifiers:
            chip_label = f"{chip_label} ({', '.join(qualifiers)})"
        title = (
            f"Scenario: {scenario_label} | Server: {server_label} | "
            f"Case status: {status or 'unknown'}"
        )
        if sources:
            title += f" | Observed in: {', '.join(sources)}"
        href = f"../cases/{quote(str(reference.get('case_name') or ''))}.html"
        chips.append(
            f"<a class='chip' href='{html.escape(href, quote=True)}' "
            f"title='{html.escape(title, quote=True)}'>{html.escape(chip_label)}</a>"
        )
    return f"<div class='flex flex-wrap gap-1.5'>{''.join(chips)}</div>"


def _declared_in_chips(references: list[dict[str, Any]]) -> str:
    """Render where a format was declared, one chip per scenario.

    A declaration belongs to the client and the scenario; the server merely
    witnessed it, so repeating it per server (or per host in a merged run) would
    only add indistinguishable chips.
    """
    by_scenario: dict[str, dict[str, Any]] = {}
    for reference in references:
        by_scenario.setdefault(str(reference.get("scenario_id") or ""), reference)
    return _case_chips(list(by_scenario.values()), qualify=False)


def _exercised_by_chips(references: list[dict[str, Any]]) -> str:
    """Render which cases negotiated a format, kept per case.

    Unlike a declaration, the server chose the format, so each case is a
    distinct observation and must not be collapsed.
    """
    return _case_chips(references, qualify=True)


def _declared_format_rows(formats: list[dict[str, Any]]) -> str:
    cell_style = "border-color: rgb(var(--retro-line) / 0.28); vertical-align: top"
    rows: list[str] = []
    for audio_format in formats:
        exercised = bool(audio_format.get("exercised"))
        claim_class = CLAIM_PILL_EXERCISED if exercised else CLAIM_PILL_UNVERIFIED
        claim_label = "Exercised" if exercised else "Not verified"
        rows.append(
            "<tr>"
            f"<td class='border-b px-4 py-3' style='{cell_style}'>"
            f"<span class='font-mono text-sm'>{_escape(audio_format.get('label'))}</span>"
            "</td>"
            f"<td class='border-b px-4 py-3' style='{cell_style}'>"
            f"<span class='{claim_class}'>{claim_label}</span>"
            "</td>"
            f"<td class='border-b px-4 py-3' style='{cell_style}'>"
            f"{_declared_in_chips(_dict_list(audio_format.get('declared_by')))}"
            "</td>"
            f"<td class='border-b px-4 py-3' style='{cell_style}'>"
            f"{_exercised_by_chips(_dict_list(audio_format.get('exercised_by')))}"
            "</td>"
            "</tr>"
        )
    return "".join(rows)


def _unreadable_declarations_notice(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    items = "".join(
        "<li>"
        f"<span class='font-mono text-[13px]'>{_escape(json.dumps(entry.get('value')))}</span> "
        f"&mdash; {_escape(entry.get('reason'))}"
        f"{_declared_in_chips(_dict_list(entry.get('declared_by')))}"
        "</li>"
        for entry in entries
    )
    return (
        "<div class='mt-4 surface-inset px-4 py-3 text-sm'>"
        "<p class='eyebrow'>Unreadable declarations</p>"
        "<p class='mt-2 subtle-copy'>These advertised entries could not be read as an audio "
        "format, so they are reported rather than dropped.</p>"
        f"<ul class='mt-2 list-disc space-y-1 pl-5 subtle-copy'>{items}</ul>"
        "</div>"
    )


def _render_declared_formats_section(entry: dict[str, Any], *, label: str) -> str:
    formats = _dict_list(entry.get("formats"))
    exercised_count = sum(1 for audio_format in formats if audio_format.get("exercised"))
    unverified_count = len(formats) - exercised_count

    header_cell = (
        "<th scope='col' class='border-b px-4 py-3 text-left' "
        "style='border-color: rgb(var(--retro-line) / 0.36); "
        "background-color: rgb(var(--retro-shell) / 0.88)'>"
        "<p class='eyebrow'>{column}</p>"
        "</th>"
    )
    headers = "".join(header_cell.format(column=column) for column in DECLARED_FORMAT_COLUMNS)

    pills = ""
    if exercised_count:
        pills += f"<span class='{CLAIM_PILL_EXERCISED}'>{exercised_count} exercised</span>"
    if unverified_count:
        pills += f"<span class='{CLAIM_PILL_UNVERIFIED}'>{unverified_count} not verified</span>"

    table = (
        ""
        if not formats
        else (
            "<section class='list-shell overflow-hidden'>"
            "<div class='overflow-x-auto'>"
            "<table class='w-full border-separate border-spacing-0 text-sm'>"
            f"<thead><tr>{headers}</tr></thead>"
            f"<tbody>{_declared_format_rows(formats)}</tbody>"
            "</table>"
            "</div>"
            "</section>"
        )
    )

    return (
        "<section class='surface p-5 sm:p-6'>"
        "<div class='flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between'>"
        "<div class='max-w-3xl'>"
        "<p class='eyebrow'>Declared capability</p>"
        "<h2 class='mt-2 text-2xl sm:text-3xl'>Declared audio formats</h2>"
        "<p class='mt-3 text-sm leading-6 subtle-copy sm:text-base'>"
        f"Every audio format the {_escape(label)} client advertised in a "
        "<span class='font-mono text-[13px]'>client/hello</span> captured by a server in this "
        "report, and whether any case negotiated exactly that codec, sample rate, bit depth and "
        "channel count. <strong>Not verified</strong> means no case ever carried the format, so "
        "the claim is untested &mdash; neither a pass nor a failure. This table compares a claim "
        "against the wire only; it does not say whether a scenario got the format it set out to "
        "test."
        "</p>"
        "</div>"
        f"<div class='flex flex-wrap items-center gap-2 xl:justify-end'>{pills}</div>"
        "</div>"
        "<div class='mt-5'>"
        f"{table}"
        f"{_unreadable_declarations_notice(_dict_list(entry.get('unreadable_declarations')))}"
        "</div>"
        "</section>"
    )


def _render_implementation_page(
    implementation: str,
    results: list[dict[str, Any]],
    *,
    declared_formats: dict[str, Any] | None = None,
) -> str:
    filtered_results = _implementation_results(results, implementation)
    label = _implementation_label(implementation)
    scenario_groups = _scenario_results(filtered_results)
    counts = _status_counts(filtered_results)
    repo_url = _implementation_repo_url(implementation)
    actions = f"<a class='chip' href='../index.html'>Full overview</a>"
    if repo_url is not None:
        actions += _external_chip(f"{label} repository", repo_url)

    sections: list[str] = []
    for scenario_id, scenario_results in scenario_groups:
        scenario_counts = _status_counts(scenario_results)
        server_focus_results = [
            result
            for result in scenario_results
            if str(result["server_impl"]) == implementation
        ]
        client_focus_results = [
            result
            for result in scenario_results
            if str(result["client_impl"]) == implementation
            and str(result["server_impl"]) != implementation
        ]

        focus_matrices: list[str] = []
        if server_focus_results:
            focus_matrices.append(
                _render_matrix(
                    server_focus_results,
                    caption=f"{label} acting as the server.",
                    href_builder=lambda result: "../" + _case_href(result),
                    server_implementations=[implementation],
                    client_implementations=_ordered_implementation_subset(
                        {str(result["client_impl"]) for result in server_focus_results}
                    ),
                )
            )
        if client_focus_results:
            focus_matrices.append(
                _render_matrix(
                    client_focus_results,
                    caption=f"{label} acting as the client.",
                    href_builder=lambda result: "../" + _case_href(result),
                    server_implementations=_ordered_implementation_subset(
                        {str(result["server_impl"]) for result in client_focus_results}
                    ),
                    client_implementations=[implementation],
                )
            )

        focus_matrices_html = ''.join(focus_matrices) if focus_matrices else '<div class="px-1 py-1 text-sm subtle-copy">No matching role-specific matrices were produced for this test.</div>'
        sections.append(
            "<section class='surface p-5 sm:p-6'>"
            "<div class='flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between'>"
            "<div class='max-w-3xl'>"
            "<p class='eyebrow'>Test</p>"
            f"<h2 class='mt-2 text-2xl sm:text-3xl'>{html.escape(_scenario_name(scenario_id))}</h2>"
            f"<p class='mt-3 text-sm leading-6 subtle-copy sm:text-base'>{html.escape(_scenario_description(scenario_id))}</p>"
            "</div>"
            "<div class='flex flex-wrap items-center gap-2 xl:justify-end'>"
            f"{_status_pill_row(scenario_counts)}"
            f"{_external_chip('View test source', _scenario_source_url(scenario_id))}"
            f"<a class='chip' href='../{html.escape(_scenario_href(scenario_id), quote=True)}'>Scenario page</a>"
            "</div>"
            "</div>"
            "<div class='mt-5 space-y-4'>"
            f"{focus_matrices_html}"
            "</div>"
            "</section>"
        )

    impl_sections_html = ''.join(sections) if sections else '<section class="surface p-6 text-sm subtle-copy">No results were found for this implementation in the current report.</section>'
    declared_formats_html = (
        _render_declared_formats_section(declared_formats, label=label)
        if declared_formats is not None
        and (declared_formats.get("formats") or declared_formats.get("unreadable_declarations"))
        else ""
    )
    body = (
        "<div class='app-shell'>"
        "<div class='mx-auto max-w-[1540px] px-4 py-4 sm:px-6 lg:px-8 lg:py-6'>"
        "<main class='space-y-6'>"
        f"{_page_header(accent='overview', breadcrumb=_breadcrumb([('Overview', '../index.html'), (label, None)]), kicker='Implementation', title=f'{label} filtered overview', description=f'This view narrows the conformance overview to cases where {label} participates as either the server or the client.', actions=actions, meta=_summary_cards(counts=counts, total_label='matching cases', total_value=len(filtered_results)))}"
        f"{declared_formats_html}"
        f"{impl_sections_html}"
        "</main>"
        "</div>"
        "</div>"
    )
    return _page_shell(
        title=f"Sendspin Conformance · {label}",
        body=body,
    )


def _render_scenario_page(
    scenario_id: str,
    results: list[dict[str, Any]],
    *,
    all_scenarios: list[tuple[str, list[dict[str, Any]]]],
) -> str:
    counts = _status_counts(results)
    ordered_results = sorted(results, key=lambda result: _case_key(result))
    case_rows = []
    for result in ordered_results:
        server_impl = str(result["server_impl"])
        client_impl = str(result["client_impl"])
        case_rows.append(
            f"<a class='list-row' href='../{html.escape(_case_href(result), quote=True)}'>"
            "<div class='grid gap-3 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1.2fr)_auto] lg:items-start'>"
            "<div class='min-w-0'>"
            "<p class='eyebrow'>Server</p>"
            f"<p class='mt-1 text-base font-semibold'>{html.escape(_implementation_label(server_impl))}</p>"
            "</div>"
            "<div class='min-w-0'>"
            "<p class='eyebrow'>Client</p>"
            f"<p class='mt-1 text-base font-semibold'>{html.escape(_implementation_label(client_impl))}</p>"
            "</div>"
            "<div class='flex items-start justify-between gap-3 lg:justify-end'>"
            f"<span class='status-pill {_status_classes(_display_status(result))}'>{html.escape(_status_label(_display_status(result)))}</span>"
            "</div>"
            "</div>"
            f"<p class='mt-3 text-sm leading-6 subtle-copy'>{html.escape(str(result['reason']))}</p>"
            "</a>"
        )

    breadcrumb = _breadcrumb(
        [
            ("Overview", "../index.html"),
            (_scenario_name(scenario_id), None),
        ]
    )
    header_meta = _summary_cards(
        counts=counts,
        total_label="pairings in this test",
        total_value=len(results),
    )
    scenario_actions = _external_chip("View test source", _scenario_source_url(scenario_id))
    scenario_stat_rows = [
        (
            "<div class='keyval-row'>"
            "<span class='text-sm muted-copy'>Pairings</span>"
            f"<span class='text-sm font-semibold'>{len(results)}</span>"
            "</div>"
        ),
        (
            "<div class='keyval-row'>"
            "<span class='text-sm muted-copy'>Passed</span>"
            f"<span class='text-sm font-semibold'>{counts.get('passed', 0)}</span>"
            "</div>"
        ),
    ]
    if counts.get("unsupported", 0):
        scenario_stat_rows.append(
            "<div class='keyval-row'>"
            "<span class='text-sm muted-copy'>Unsupported</span>"
            f"<span class='text-sm font-semibold'>{counts.get('unsupported', 0)}</span>"
            "</div>"
        )
    if counts.get("failed", 0):
        scenario_stat_rows.append(
            "<div class='keyval-row'>"
            "<span class='text-sm muted-copy'>Failed</span>"
            f"<span class='text-sm font-semibold'>{counts.get('failed', 0)}</span>"
            "</div>"
        )
    if counts.get("skipped", 0):
        scenario_stat_rows.append(
            "<div class='keyval-row'>"
            "<span class='text-sm muted-copy'>Skipped</span>"
            f"<span class='text-sm font-semibold'>{counts.get('skipped', 0)}</span>"
            "</div>"
        )
    case_rows_html = ''.join(case_rows) if case_rows else '<div class="px-5 py-5 text-sm subtle-copy">No cases were written for this scenario.</div>'
    body = (
        "<div class='app-shell'>"
        "<div class='mx-auto max-w-[1360px] px-4 py-4 sm:px-6 lg:px-8 lg:py-6'>"
        "<div class='grid gap-6 xl:grid-cols-[280px_minmax(0,1fr)]'>"
        "<aside class='sidebar-rail sidebar-rail-xl'>"
        f"{_sidebar_nav(('Back to overview', '../index.html', 'Return to the full matrix list.'))}"
        f"{_sidebar_resources(scenario_id=scenario_id)}"
        "<section class='surface p-5'>"
        "<p class='eyebrow'>This test</p>"
        f"<h2 class='mt-2 text-xl'>{html.escape(_scenario_name(scenario_id))}</h2>"
        f"<p class='mt-3 text-sm leading-6 subtle-copy'>{html.escape(_scenario_intro(scenario_id))}</p>"
        "<div class='keyval mt-4'>"
        f"{''.join(scenario_stat_rows)}"
        "</div>"
        "</section>"
        f"{_nav_scenarios(all_scenarios, current_scenario_id=scenario_id, href_prefix='../')}"
        "</aside>"
        "<main class='space-y-6'>"
        f"{_page_header(accent='scenario', breadcrumb=breadcrumb, kicker='Test', title=_scenario_name(scenario_id), description=_scenario_description(scenario_id), actions=scenario_actions, meta=header_meta)}"
        "<section class='surface overflow-hidden'>"
        "<div class='border-b px-5 py-5 sm:px-6' style='border-color: rgb(var(--retro-line) / 0.38)'>"
        "<p class='eyebrow'>Runs</p>"
        "<h2 class='mt-2 text-2xl'>Server and client pairings</h2>"
        "<p class='mt-2 max-w-3xl text-sm leading-6 subtle-copy'>"
        "Open a case to inspect the full hello payloads, summaries, logs, and build output for that pairing."
        "</p>"
        "</div>"
        "<div class='space-y-4 p-4 sm:p-5'>"
        "<section class='surface-inset overflow-hidden'>"
        "<div class='list-shell rounded-none border-0 shadow-none'>"
        f"{case_rows_html}"
        "</div>"
        "</section>"
        "</div>"
        "</section>"
        "</main>"
        "</div>"
        "</div>"
        "</div>"
    )
    return _page_shell(
        title=f"Sendspin Conformance · {_scenario_name(scenario_id)}",
        body=body,
    )


def _render_case_page(
    result: dict[str, Any],
    *,
    data_dir: Path,
    build_index: dict[tuple[str, str], dict[str, Any]],
) -> str:
    scenario_id = str(result["scenario_id"])
    environment_id, _ = _result_environment(result)
    case_name = _case_slug(result)
    payload = _case_payload(result, data_dir=data_dir)
    case_dir = payload["case_dir"]
    server_impl = str(result["server_impl"])
    client_impl = str(result["client_impl"])
    status = _display_status(result)
    server_label = _implementation_label(server_impl)
    client_label = _implementation_label(client_impl)

    summary_tab = f"{case_name}--summary"
    server_tab = f"{case_name}--server"
    client_tab = f"{case_name}--client"
    builds_tab = f"{case_name}--builds"
    build_panels = _build_panels_for_case(
        data_dir=data_dir,
        environment_id=environment_id,
        server_impl=server_impl,
        client_impl=client_impl,
        build_index=build_index,
    )
    has_build_tab = bool(build_panels)
    build_tab_button = (
        f"<button type='button' class='tab-button tab-idle' data-tab-button='{html.escape(builds_tab, quote=True)}' aria-selected='false'>Builds</button>"
        if has_build_tab
        else ""
    )
    inspection_title = "Summaries, logs, and builds" if has_build_tab else "Summaries and logs"
    build_tab_panel = (
        f"<section class='mt-4 space-y-4' data-tab-panel='{html.escape(builds_tab, quote=True)}' hidden>{build_panels}</section>"
        if has_build_tab
        else ""
    )

    breadcrumb = _breadcrumb(
        [
            ("Overview", "../index.html"),
            (_scenario_name(scenario_id), "../" + _scenario_href(scenario_id)),
            ("Case", None),
        ]
    )
    header_meta = (
        "<div class='grid gap-3 lg:grid-cols-3'>"
        "<div class='detail-card'>"
        "<p class='eyebrow'>Server</p>"
        f"{_implementation_identity(server_impl, primary_class='mt-2 text-lg font-semibold', secondary_class='mt-1 text-sm muted-copy')}"
        "</div>"
        "<div class='detail-card'>"
        "<p class='eyebrow'>Client</p>"
        f"{_implementation_identity(client_impl, primary_class='mt-2 text-lg font-semibold', secondary_class='mt-1 text-sm muted-copy')}"
        "</div>"
        "<div class='detail-card'>"
        "<p class='eyebrow'>Status</p>"
        f"<p class='mt-2'><span class='status-pill {_status_classes(status)}'>{html.escape(_status_label(status))}</span></p>"
        f"<p class='mt-3 text-sm muted-copy'>{html.escape(str(result['reason']))}</p>"
        "</div>"
        "</div>"
    )
    body = (
        "<div class='app-shell'>"
        "<div class='mx-auto max-w-[1320px] px-4 py-4 sm:px-6 lg:px-8 lg:py-6'>"
        "<div class='grid gap-6 xl:grid-cols-[300px_minmax(0,1fr)]'>"
        "<aside class='sidebar-rail sidebar-rail-xl'>"
        f"{_sidebar_nav(('Back to test', '../' + _scenario_href(scenario_id), 'Return to the pairing list for this test.'), ('Back to overview', '../index.html', 'Return to the matrix-first overview.'))}"
        "<section class='surface p-5'>"
        "<p class='eyebrow'>Run facts</p>"
        "<div class='keyval mt-4'>"
        "<div class='keyval-row keyval-row-stack'>"
        "<span class='text-sm muted-copy'>Case id</span>"
        f"<span class='text-sm font-semibold'>{html.escape(case_name)}</span>"
        "</div>"
        "<div class='keyval-row keyval-row-stack'>"
        "<span class='text-sm muted-copy'>Server exit</span>"
        f"<span class='text-sm font-semibold'>{html.escape(str(result.get('server_exit_code')))}</span>"
        "</div>"
        "<div class='keyval-row keyval-row-stack'>"
        "<span class='text-sm muted-copy'>Client exit</span>"
        f"<span class='text-sm font-semibold'>{html.escape(str(result.get('client_exit_code')))}</span>"
        "</div>"
        "<div class='keyval-row keyval-row-stack'>"
        "<span class='text-sm muted-copy'>Test</span>"
        f"<span class='text-sm font-semibold'>{html.escape(_scenario_name(scenario_id))}</span>"
        "</div>"
        "</div>"
        "</section>"
        f"{_sidebar_resources(scenario_id=scenario_id)}"
        "</aside>"
        "<main class='space-y-6'>"
        f"{_page_header(accent='case', breadcrumb=breadcrumb, kicker='Case', title=f'{server_label} -> {client_label}', description=_scenario_description(scenario_id), meta=header_meta)}"
        "<section class='surface p-4 sm:p-5' data-tabset>"
        "<div>"
        "<p class='eyebrow'>Inspection</p>"
        f"<h2 class='mt-2 text-2xl'>{html.escape(inspection_title)}</h2>"
        "</div>"
        "<div class='tab-strip mt-4'>"
        f"<button type='button' class='tab-button tab-idle' data-tab-button='{html.escape(summary_tab, quote=True)}' data-default-tab='true' aria-selected='false'>Summary</button>"
        f"<button type='button' class='tab-button tab-idle' data-tab-button='{html.escape(server_tab, quote=True)}' aria-selected='false'>Server: {html.escape(server_label)}</button>"
        f"<button type='button' class='tab-button tab-idle' data-tab-button='{html.escape(client_tab, quote=True)}' aria-selected='false'>Client: {html.escape(client_label)}</button>"
        f"{build_tab_button}"
        "</div>"
        "<section class='mt-4 space-y-4' data-tab-panel='"
        f"{html.escape(summary_tab, quote=True)}"
        "'>"
        f"{_render_code_panel(heading='Matrix result', content=str(payload['result_json']), mode='json', raw_href='../data/' + case_name + '/result.json')}"
        "</section>"
        "<section class='mt-4 space-y-4' data-tab-panel='"
        f"{html.escape(server_tab, quote=True)}"
        "' hidden>"
        "<div class='grid gap-4 2xl:grid-cols-2'>"
        f"{_render_code_panel(heading='Server summary (JSON)', content=payload['server_summary_json'], mode='json', raw_href='../data/' + case_name + '/server-summary.json' if (case_dir / 'server-summary.json').exists() else None)}"
        f"{_render_code_panel(heading='Server log', content=payload['server_log'], mode='log', raw_href='../data/' + case_name + '/server.log' if (case_dir / 'server.log').exists() else None)}"
        "</div>"
        "</section>"
        "<section class='mt-4 space-y-4' data-tab-panel='"
        f"{html.escape(client_tab, quote=True)}"
        "' hidden>"
        "<div class='grid gap-4 2xl:grid-cols-2'>"
        f"{_render_code_panel(heading='Client summary (JSON)', content=payload['client_summary_json'], mode='json', raw_href='../data/' + case_name + '/client-summary.json' if (case_dir / 'client-summary.json').exists() else None)}"
        f"{_render_code_panel(heading='Client log', content=payload['client_log'], mode='log', raw_href='../data/' + case_name + '/client.log' if (case_dir / 'client.log').exists() else None)}"
        "</div>"
        "</section>"
        f"{build_tab_panel}"
        "</section>"
        "</main>"
        "</div>"
        "</div>"
        "</div>"
    )
    return _page_shell(
        title=f"Sendspin Conformance · {_scenario_name(scenario_id)} · {server_label} to {client_label}",
        body=body,
        include_tabs=True,
    )


def build_site(results_dir: Path, site_dir: Path) -> None:
    """Generate a static report site with scenario and case pages."""
    data_dir = _data_dir(results_dir)
    results_payload = read_json(data_dir / "index.json")
    results: list[dict[str, Any]] = list(results_payload["results"])
    build_index = _build_results_index(data_dir)

    # Written before the artifact sync so a site directory separate from the
    # results directory receives it along with the other cross-run metadata.
    declared_formats = collect_declared_formats(data_dir, results)
    write_json(data_dir / "declared-formats.json", declared_formats)

    site_dir.mkdir(parents=True, exist_ok=True)
    _clear_legacy_dirs(site_dir)
    _sync_case_artifacts(data_dir, site_dir)
    scenarios_dir = _prepare_output_dir(site_dir, "scenarios")
    cases_dir = _prepare_output_dir(site_dir, "cases")
    implementations_dir = _prepare_output_dir(site_dir, "implementations")

    scenario_groups = _scenario_results(results)

    (site_dir / "index.html").write_text(
        _render_index_page(results, data_dir=data_dir),
        encoding="utf-8",
    )

    for scenario_id, scenario_results in scenario_groups:
        (scenarios_dir / f"{scenario_id}.html").write_text(
            _render_scenario_page(
                scenario_id,
                scenario_results,
                all_scenarios=scenario_groups,
            ),
            encoding="utf-8",
        )
        for result in scenario_results:
            (cases_dir / f"{_case_slug(result)}.html").write_text(
                _render_case_page(
                    result,
                    data_dir=data_dir,
                    build_index=build_index,
                ),
                encoding="utf-8",
            )

    for implementation in implementation_names():
        implementation_results = _implementation_results(results, implementation)
        if not implementation_results:
            continue
        (implementations_dir / f"{implementation}.html").write_text(
            _render_implementation_page(
                implementation,
                results,
                declared_formats=declared_formats_for_implementation(
                    declared_formats,
                    implementation,
                ),
            ),
            encoding="utf-8",
        )
