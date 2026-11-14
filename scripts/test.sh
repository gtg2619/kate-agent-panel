#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="${KATE_AGENT_BUILD_DIR:-$project_dir/build}"
sdk_dir="${KATE_AGENT_SDK:-/tmp/kate-agent-sdk}"
mode="${1:-all}"
case "$mode" in
    all|--unit) [[ $# -le 1 ]] || { echo 'Unexpected arguments' >&2; exit 2; } ;;
    --live) [[ $# == 2 && ( "$2" == deepseek || "$2" == zcode ) ]] || { echo 'Usage: scripts/test.sh --live deepseek|zcode' >&2; exit 2; } ;;
    *) echo 'Usage: scripts/test.sh [--unit | --live deepseek|zcode]' >&2; exit 2 ;;
esac
cd "$project_dir"
# Normal tests must never inherit an opt-in to paid model calls.
unset KATE_AGENT_LIVE_TEST
test_node="${KATE_AGENT_TEST_NODE:-$(command -v node || true)}"
if [[ -z "$test_node" ]]; then
    echo 'Tests require Node.js 22.15+ or 24+ (module.registerHooks).' >&2
    exit 1
fi
"$test_node" -e "if (typeof require('node:module').registerHooks !== 'function') { console.error('Tests require Node.js 22.15+ or 24+ (module.registerHooks).'); process.exit(1); }"
export KATE_AGENT_TEST_NODE="$test_node"
if [[ "$mode" == --unit ]]; then
    python3 -m unittest discover -s tests -p 'test_*.py' -v
    "$test_node" tests/renderer_test.cjs
    exit
fi
mkdir -p "$build_dir"
build_dir="$(cd -- "$build_dir" && pwd)"
# Serialize native builds and Xvfb use within this build directory.
exec 9>"$build_dir/.test.lock"
flock 9
cmake_args=(-S "$project_dir" -B "$build_dir" -DBUILD_TESTING=ON -DCMAKE_BUILD_TYPE=Debug
    -DNODE_EXECUTABLE="$test_node")
if [[ -f "$sdk_dir/usr/lib64/libQt6WebEngineCore.so.6" ]]; then
    cmake_args+=(-DCMAKE_PREFIX_PATH="$sdk_dir/usr")
    export LD_LIBRARY_PATH="$sdk_dir/usr/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export QTWEBENGINEPROCESS_PATH="$sdk_dir/usr/lib64/qt6/libexec/QtWebEngineProcess"
    export QTWEBENGINE_RESOURCES_PATH="$sdk_dir/usr/share/qt6/resources"
    export QTWEBENGINE_LOCALES_PATH="$sdk_dir/usr/share/qt6/translations/qtwebengine_locales"
fi
cmake "${cmake_args[@]}"
cmake --build "$build_dir" --parallel "${KATE_AGENT_BUILD_JOBS:-4}"
export QTWEBENGINE_CHROMIUM_FLAGS="${QTWEBENGINE_CHROMIUM_FLAGS:---disable-gpu}"
run=()
if command -v xvfb-run >/dev/null; then
    run=(xvfb-run -a env QT_QPA_PLATFORM=xcb)
elif [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    echo 'Native tests require Xvfb or an accessible display server.' >&2
    exit 1
fi
if [[ "$mode" == --live ]]; then
    export KATE_AGENT_LIVE_TEST="$2"
    "${run[@]}" "$build_dir/bin/panel_test" liveConversation liveQuestionsAndResume
else
    "${run[@]}" ctest --test-dir "$build_dir" --output-on-failure --output-junit "$build_dir/test-results.xml"
fi
