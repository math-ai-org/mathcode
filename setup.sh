#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_REPO="math-ai-org/mathcode"
RELEASE_TAG="v0.3.0"
RELEASE_RUNTIME_GENERATION="v0.3.0-optional-lean-1"
LOCAL_ELAN_HOME="$ROOT_DIR/.local/elan"
LOCAL_ELAN_BIN="$LOCAL_ELAN_HOME/bin"
LEAN_WORKSPACE_DIR="$ROOT_DIR/lean-workspace"
LEAN_DEFERRED_MARKER="$ROOT_DIR/.mathcode-lean-deferred"
LEAN_READY_MARKER="$ROOT_DIR/.mathcode-lean-ready"
LEAN_INSTALL_LOCK_DIR="$ROOT_DIR/.mathcode-lean-install.lock"
LOCAL_RELEASE_ASSET_DIR="$ROOT_DIR/release-assets"
MATHLIB_CACHE_MIN_KB=$((8 * 1024 * 1024))
MATHCODE_COMMAND_AVAILABLE_NOW=0
MATHCODE_USER_LAUNCHER_INSTALLED=0
MATHCODE_COMMAND_READY_AFTER_RELOAD=0
USING_SYSTEM_LEAN=0
MATHCODE_LAUNCHER_MARKER="# MathCode launcher (managed by setup.sh)"
LEAN_INSTALL_LOCK_HELD=0
LEAN_INSTALL_LOCK_TOKEN=""
LEAN_INSTALL_LOCK_WAITED=0
USE_LOCAL_RELEASE_ASSETS=0

resolve_command_path() {
  local cmd_path="$1"
  case "$cmd_path" in
    /*) printf '%s\n' "$cmd_path" ;;
    */*) printf '%s\n' "$(cd -- "$(dirname -- "$cmd_path")" && pwd)/$(basename -- "$cmd_path")" ;;
    *) printf '%s\n' "$cmd_path" ;;
  esac
}

INITIAL_LEAN_CMD="$(command -v lean 2>/dev/null || true)"
if [[ -n "$INITIAL_LEAN_CMD" ]]; then
  INITIAL_LEAN_CMD="$(resolve_command_path "$INITIAL_LEAN_CMD")"
fi
INITIAL_LAKE_CMD="$(command -v lake 2>/dev/null || true)"
if [[ -n "$INITIAL_LAKE_CMD" ]]; then
  INITIAL_LAKE_CMD="$(resolve_command_path "$INITIAL_LAKE_CMD")"
fi
INITIAL_CURL_CMD="$(command -v curl 2>/dev/null || true)"
if [[ -n "$INITIAL_CURL_CMD" ]]; then
  INITIAL_CURL_CMD="$(resolve_command_path "$INITIAL_CURL_CMD")"
fi
LEAN_CMD=""
LAKE_CMD=""
CURL_CMD=""

cd "$ROOT_DIR"

add_to_path() {
  local dir="$1"
  [[ -d "$dir" ]] || return 0
  case ":$PATH:" in
    *":$dir:"*) ;;
    *) export PATH="$dir:$PATH" ;;
  esac
}

prepend_to_path() {
  local dir="$1"
  local component next_path rest
  [[ -d "$dir" ]] || return 0
  if [[ -z "${PATH:-}" ]]; then
    export PATH="$dir"
    return 0
  fi
  next_path="$dir"
  rest="$PATH"
  while [[ "$rest" == *:* ]]; do
    component="${rest%%:*}"
    rest="${rest#*:}"
    [[ "$component" == "$dir" ]] || next_path="$next_path:$component"
  done
  [[ "$rest" == "$dir" ]] || next_path="$next_path:$rest"
  export PATH="$next_path"
}

local_elan_artifacts_present() {
  [[ -e "$LOCAL_ELAN_BIN/elan" || -e "$LOCAL_ELAN_BIN/elan.exe" || \
     -e "$LOCAL_ELAN_BIN/lean" || -e "$LOCAL_ELAN_BIN/lean.exe" || \
     -e "$LOCAL_ELAN_BIN/lake" || -e "$LOCAL_ELAN_BIN/lake.exe" ]]
}

lean_ready_marker_matches_release() {
  [[ -f "$LEAN_READY_MARKER" && ! -L "$LEAN_READY_MARKER" ]] &&
    grep -Fxq "release_tag=$RELEASE_TAG" "$LEAN_READY_MARKER" 2>/dev/null
}

lean_runtime_pair_available_for_status() {
  if local_elan_tool_path lean >/dev/null && local_elan_tool_path lake >/dev/null; then
    return 0
  fi
  if use_system_lean_requested && \
     { [[ -n "$INITIAL_LEAN_CMD" && -n "$INITIAL_LAKE_CMD" ]] || \
       { have_command lean && have_command lake; }; }; then
    return 0
  fi
  return 1
}

use_system_lean_requested() {
  [[ "${MATHCODE_SETUP_USE_SYSTEM_LEAN:-0}" == "1" ]]
}

path_contains_dir() {
  local dir="$1"
  case ":$PATH:" in
    *":$dir:"*) return 0 ;;
    *) return 1 ;;
  esac
}

have_command() {
  command -v "$1" &>/dev/null
}

ensure_curl() {
  if [[ -n "$CURL_CMD" ]]; then
    return
  fi

  if [[ -n "$INITIAL_CURL_CMD" ]]; then
    CURL_CMD="$INITIAL_CURL_CMD"
    return
  fi

  if CURL_CMD="$(command -v curl 2>/dev/null)"; then
    CURL_CMD="$(resolve_command_path "$CURL_CMD")"
    return
  fi

  log "curl is required to download MathCode release assets or install Lean locally."
  exit 1
}

is_executable_file() {
  [[ -f "$1" && -x "$1" ]]
}

local_elan_tool_path() {
  local tool="$1"
  if is_executable_file "$LOCAL_ELAN_BIN/$tool"; then
    printf '%s\n' "$LOCAL_ELAN_BIN/$tool"
    return 0
  fi
  if is_executable_file "$LOCAL_ELAN_BIN/$tool.exe"; then
    printf '%s\n' "$LOCAL_ELAN_BIN/$tool.exe"
    return 0
  fi
  return 1
}

log() {
  printf '%s\n' "$1"
}

shell_quote() {
  printf "'"
  printf '%s' "$1" | sed "s/'/'\\\\''/g"
  printf "'"
}

env_file_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\$/\\\$}"
  value="${value//\`/\\\`}"
  printf '"%s"' "$value"
}

encode_env_file_path() {
  local encoded
  if ! encoded="$(printf '%s' "$1" | base64 | tr -d '\r\n')"; then
    return 1
  fi
  [[ -n "$encoded" ]] || return 1
  printf 'base64:%s' "$encoded"
}

normalize_install_bin_dir() {
  local dir="$1"

  case "$dir" in
    "~")
      [[ -n "${HOME:-}" ]] || return 0
      printf '%s\n' "$HOME"
      ;;
    "~/"*)
      [[ -n "${HOME:-}" ]] || return 0
      printf '%s\n' "$HOME/${dir#~/}"
      ;;
    /*)
      printf '%s\n' "$dir"
      ;;
    *)
      printf '%s\n' "$ROOT_DIR/$dir"
      ;;
  esac
}

available_kb() {
  local path="$1"
  local value
  if ! value="$(df -Pk "$path" 2>/dev/null | awk 'NR==2 { print $4 }')"; then
    return 0
  fi
  case "$value" in
    ''|*[!0-9]*) return 0 ;;
    *) printf '%s\n' "$value" ;;
  esac
}

default_install_bin_dir() {
  if [[ -n "${MATHCODE_INSTALL_BIN_DIR:-}" ]]; then
    normalize_install_bin_dir "$MATHCODE_INSTALL_BIN_DIR"
    return
  fi

  if [[ -n "${HOME:-}" ]] && path_contains_dir "$HOME/.local/bin"; then
    printf '%s\n' "$HOME/.local/bin"
    return
  fi

  if [[ -n "${HOME:-}" ]] && path_contains_dir "$HOME/bin"; then
    printf '%s\n' "$HOME/bin"
    return
  fi

  if [[ -n "${HOME:-}" ]]; then
    printf '%s\n' "$HOME/.local/bin"
    return
  fi

  printf '\n'
}

default_shell_profile() {
  case "$(basename "${SHELL:-}")" in
    zsh)
      printf '%s\n' "$HOME/.zshrc"
      ;;
    bash)
      if [[ -f "$HOME/.bash_profile" || ! -f "$HOME/.bashrc" ]]; then
        printf '%s\n' "$HOME/.bash_profile"
      else
        printf '%s\n' "$HOME/.bashrc"
      fi
      ;;
    *)
      printf '\n'
      ;;
  esac
}

render_path_block() {
  local dir="$1"
  local begin_marker="$2"
  local end_marker="$3"
  local quoted_dir

  quoted_dir="$(shell_quote "$dir")"

  cat <<EOF
$begin_marker
_mathcode_bin_dir=$quoted_dir
if [ -d "\$_mathcode_bin_dir" ]; then
  case ":\$PATH:" in
    *:"\$_mathcode_bin_dir":*) ;;
    *) export PATH="\$_mathcode_bin_dir:\$PATH" ;;
  esac
fi
unset _mathcode_bin_dir
$end_marker
EOF
}

extract_managed_block() {
  local file_path="$1"
  local begin_marker="$2"
  local end_marker="$3"

  awk -v begin="$begin_marker" -v end="$end_marker" '
    $0 == begin { in_block = 1 }
    in_block { print }
    $0 == end && in_block { exit }
  ' "$file_path"
}

managed_block_is_well_formed() {
  local file_path="$1"
  local begin_marker="$2"
  local end_marker="$3"

  awk -v begin="$begin_marker" -v end="$end_marker" '
    $0 == begin {
      begin_count++
      if (in_block) nested_begin = 1
      in_block = 1
      next
    }
    $0 == end {
      end_count++
      if (!in_block) stray_end = 1
      in_block = 0
      next
    }
    END {
      if (begin_count == 1 && end_count == 1 && !in_block && !nested_begin && !stray_end) {
        exit 0
      }
      exit 1
    }
  ' "$file_path"
}

replace_managed_block() {
  local file_path="$1"
  local begin_marker="$2"
  local end_marker="$3"
  local desired_block="$4"
  local temp_path

  if ! temp_path="$(mktemp "${TMPDIR:-/tmp}/mathcode-profile.XXXXXX")"; then
    return 1
  fi
  if ! awk -v begin="$begin_marker" -v end="$end_marker" '
    $0 == begin { in_block = 1; next }
    in_block && $0 == end { in_block = 0; next }
    !in_block { print }
  ' "$file_path" > "$temp_path"; then
    rm -f "$temp_path"
    return 1
  fi
  if ! printf '\n%s\n' "$desired_block" >> "$temp_path"; then
    rm -f "$temp_path"
    return 1
  fi
  if ! mv "$temp_path" "$file_path"; then
    rm -f "$temp_path"
    return 1
  fi
}

current_launcher_target() {
  printf '%s\n' "$ROOT_DIR/run"
}

current_launcher_path() {
  local install_bin_dir
  install_bin_dir="$(default_install_bin_dir)"
  [[ -n "$install_bin_dir" ]] || return 0
  printf '%s\n' "$install_bin_dir/mathcode"
}

launcher_state_key() {
  printf '%s\n' "$ROOT_DIR" | cksum | awk '{print $1}'
}

launcher_state_file() {
  [[ -n "${HOME:-}" ]] || return 0
  printf '%s/.mathcode/launcher-state/%s.path\n' "$HOME" "$(launcher_state_key)"
}

read_recorded_launcher_path() {
  local state_file recorded_launcher_path

  state_file="$(launcher_state_file)"
  [[ -n "$state_file" && -f "$state_file" ]] || return 0

  IFS= read -r recorded_launcher_path < "$state_file" || true
  [[ -n "$recorded_launcher_path" ]] || return 0
  printf '%s\n' "$recorded_launcher_path"
}

record_launcher_path() {
  local launcher_path="$1"
  local state_file

  state_file="$(launcher_state_file)"
  [[ -n "$state_file" ]] || return 0
  if ! mkdir -p "$(dirname "$state_file")"; then
    return 1
  fi
  if ! printf '%s\n' "$launcher_path" > "$state_file"; then
    return 1
  fi
}

clear_recorded_launcher_path() {
  local state_file
  state_file="$(launcher_state_file)"
  [[ -n "$state_file" ]] || return 0
  rm -f "$state_file"
}

remove_managed_launcher_file() {
  local launcher_path="$1"
  local managed_marker="$2"
  local target_path="$3"
  local quoted_target_path

  [[ -n "$launcher_path" ]] || return 0
  [[ -f "$launcher_path" ]] || return 0

  if ! grep -Fq "$managed_marker" "$launcher_path" 2>/dev/null; then
    return 0
  fi

  quoted_target_path="$(shell_quote "$target_path")"
  if ! grep -Fq "$target_path" "$launcher_path" 2>/dev/null && \
     ! grep -Fq "$quoted_target_path" "$launcher_path" 2>/dev/null; then
    return 0
  fi

  if ! rm -f "$launcher_path"; then
    log "MathCode could not remove the previous managed launcher: $launcher_path"
    return 1
  fi
  log "Removed managed user-local \`mathcode\` launcher: $launcher_path"
}

ensure_path_loads_in_future_shells() {
  local dir="$1"
  local launcher_path="${2:-}"
  local profile_path begin_marker end_marker desired_block current_block path_has_dir=0

  if path_contains_dir "$dir"; then
    if [[ -z "$launcher_path" || "$(command -v mathcode 2>/dev/null || true)" == "$launcher_path" ]]; then
      MATHCODE_COMMAND_AVAILABLE_NOW=1
    fi
    path_has_dir=1
  fi

  profile_path="$(default_shell_profile)"
  if [[ -z "$profile_path" ]]; then
    if [[ "$path_has_dir" -eq 1 ]]; then
      return
    fi

    log "Installed user-local \`mathcode\` launcher in $dir."
    log "Add this directory to PATH manually to use \`mathcode\`: $dir"
    return
  fi

  if ! mkdir -p "$(dirname "$profile_path")"; then
    log "MathCode could not create the shell profile directory for $profile_path."
    log "Add this directory to PATH manually to use \`mathcode\` in new shells: $dir"
    return
  fi
  begin_marker="# >>> MathCode user bin >>>"
  end_marker="# <<< MathCode user bin <<<"
  desired_block="$(render_path_block "$dir" "$begin_marker" "$end_marker")"

  if [[ -f "$profile_path" ]] && { grep -Fq "$begin_marker" "$profile_path" || grep -Fq "$end_marker" "$profile_path"; }; then
    if ! managed_block_is_well_formed "$profile_path" "$begin_marker" "$end_marker"; then
      log "MathCode PATH block markers are malformed in $profile_path; leaving the profile unchanged."
      log "Fix or remove the broken block, then rerun setup if you want MathCode to manage PATH there."
      return
    fi

    current_block="$(extract_managed_block "$profile_path" "$begin_marker" "$end_marker")"
    if [[ "$current_block" == "$desired_block" ]]; then
      MATHCODE_COMMAND_READY_AFTER_RELOAD=1
      log "mathcode launcher directory is already managed in $profile_path."
      log "Open a new shell or run: source $profile_path"
      return
    fi

    if ! replace_managed_block "$profile_path" "$begin_marker" "$end_marker" "$desired_block"; then
      log "MathCode could not update PATH management in $profile_path."
      log "Add this directory to PATH manually to use \`mathcode\` in new shells: $dir"
      return
    fi
    MATHCODE_COMMAND_READY_AFTER_RELOAD=1
    log "Updated PATH management for $dir in $profile_path."
    log "Open a new shell or run: source $profile_path"
    return
  fi

  if ! printf '\n%s\n' "$desired_block" >> "$profile_path"; then
    log "MathCode could not append PATH management to $profile_path."
    log "Add this directory to PATH manually to use \`mathcode\` in new shells: $dir"
    return
  fi
  MATHCODE_COMMAND_READY_AFTER_RELOAD=1
  log "Added $dir to PATH in $profile_path."
  log "Open a new shell or run: source $profile_path"
}

install_mathcode_command() {
  local install_bin_dir launcher_path target_path quoted_target recorded_launcher_path

  if [[ "${MATHCODE_SKIP_USER_BIN:-0}" == "1" ]]; then
    log "Skipping user-local \`mathcode\` launcher because MATHCODE_SKIP_USER_BIN=1."
    return
  fi

  if [[ -z "${HOME:-}" ]]; then
    log "Skipping user-local \`mathcode\` launcher because HOME is not set."
    return
  fi

  install_bin_dir="$(default_install_bin_dir)"
  if [[ -z "$install_bin_dir" ]]; then
    log "Skipping user-local \`mathcode\` launcher because no install directory could be resolved."
    return
  fi

  if [[ -e "$install_bin_dir" && ! -d "$install_bin_dir" ]]; then
    log "Skipping user-local \`mathcode\` launcher because install directory path exists and is not a directory: $install_bin_dir"
    return
  fi

  if ! mkdir -p "$install_bin_dir"; then
    log "Skipping user-local \`mathcode\` launcher because install directory could not be created: $install_bin_dir"
    return
  fi
  launcher_path="$install_bin_dir/mathcode"
  target_path="$(current_launcher_target)"
  quoted_target="$(shell_quote "$target_path")"

  if [[ -e "$launcher_path" && ! -f "$launcher_path" ]]; then
    log "Skipping install of $launcher_path because it exists and is not a regular file."
    return
  fi

  if [[ -f "$launcher_path" ]] && ! grep -Fq "$MATHCODE_LAUNCHER_MARKER" "$launcher_path" 2>/dev/null; then
    log "Skipping install of $launcher_path because it already exists and is not managed by this checkout."
    log "If you want this checkout to own the \`mathcode\` command, remove or rename that file and rerun setup."
    return
  fi

  recorded_launcher_path="$(read_recorded_launcher_path)"
  if [[ -n "$recorded_launcher_path" && "$recorded_launcher_path" != "$launcher_path" ]]; then
    if ! remove_managed_launcher_file "$recorded_launcher_path" "$MATHCODE_LAUNCHER_MARKER" "$target_path"; then
      log "Continuing with the new launcher install, but the previous managed launcher still needs manual cleanup."
    fi
  fi

  local temp_launcher_path
  if ! temp_launcher_path="$(mktemp "$install_bin_dir/.mathcode-launcher.XXXXXX")"; then
    log "Skipping user-local \`mathcode\` launcher because a temporary launcher file could not be created in: $install_bin_dir"
    return
  fi

  if ! cat > "$temp_launcher_path" <<LAUNCHER
#!/usr/bin/env bash
$MATHCODE_LAUNCHER_MARKER
set -euo pipefail
TARGET=$quoted_target
if [[ ! -x "\$TARGET" ]]; then
  printf 'Installed MathCode launcher target is missing: %s\n' "\$TARGET" >&2
  printf 'Re-run: bash setup.sh\n' >&2
  exit 1
fi
exec "\$TARGET" "\$@"
LAUNCHER
  then
    rm -f "$temp_launcher_path"
    log "Skipping user-local \`mathcode\` launcher because the launcher file could not be written: $launcher_path"
    return
  fi
  if ! chmod +x "$temp_launcher_path"; then
    rm -f "$temp_launcher_path"
    log "Skipping user-local \`mathcode\` launcher because the launcher file could not be marked executable: $launcher_path"
    return
  fi
  if ! mv -f "$temp_launcher_path" "$launcher_path"; then
    rm -f "$temp_launcher_path"
    log "Skipping user-local \`mathcode\` launcher because the launcher file could not be moved into place: $launcher_path"
    return
  fi

  if ! record_launcher_path "$launcher_path"; then
    log "MathCode could not record the managed launcher path for future status tracking; continuing with the installed launcher."
  fi
  MATHCODE_USER_LAUNCHER_INSTALLED=1
  log "Installed user-local \`mathcode\` launcher: $launcher_path"
  ensure_path_loads_in_future_shells "$install_bin_dir" "$launcher_path"
}

remove_managed_mathcode_command() {
  local target_path recorded_launcher_path computed_launcher_path removal_failed=0

  target_path="$(current_launcher_target)"
  recorded_launcher_path="$(read_recorded_launcher_path)"
  computed_launcher_path="$(current_launcher_path)"

  if [[ -n "$recorded_launcher_path" ]]; then
    if ! remove_managed_launcher_file "$recorded_launcher_path" "$MATHCODE_LAUNCHER_MARKER" "$target_path"; then
      removal_failed=1
    fi
  fi

  if [[ -n "$computed_launcher_path" && "$computed_launcher_path" != "$recorded_launcher_path" ]]; then
    if ! remove_managed_launcher_file "$computed_launcher_path" "$MATHCODE_LAUNCHER_MARKER" "$target_path"; then
      removal_failed=1
    fi
  fi

  if [[ "$removal_failed" -eq 0 ]]; then
    clear_recorded_launcher_path
  else
    log "Keeping the recorded launcher path so you can retry cleanup after fixing the filesystem permissions."
  fi
}

remove_lean_install_lock_owned_by() {
  local expected_token="$1"
  local current_token=""
  local recovery_path="$ROOT_DIR/.mathcode-lean-install.owner.$$.$RANDOM.$RANDOM"

  if [[ -L "$LEAN_INSTALL_LOCK_DIR" || ! -d "$LEAN_INSTALL_LOCK_DIR" || \
        ! -f "$LEAN_INSTALL_LOCK_DIR/owner" || -L "$LEAN_INSTALL_LOCK_DIR/owner" ]]; then
    log "Lean install lock is incomplete or unsafe; refusing to remove it."
    return 1
  fi
  IFS= read -r current_token < "$LEAN_INSTALL_LOCK_DIR/owner" || true
  if [[ "$current_token" != "$expected_token" ]]; then
    log "Lean install lock ownership changed; refusing to remove another owner's lock."
    return 1
  fi

  if ! mv "$LEAN_INSTALL_LOCK_DIR/owner" "$recovery_path"; then
    log "Could not stage the Lean install lock owner for exact-token release."
    return 1
  fi
  if rmdir "$LEAN_INSTALL_LOCK_DIR" 2>/dev/null; then
    rm -f "$recovery_path"
    return 0
  fi

  if [[ ! -L "$LEAN_INSTALL_LOCK_DIR" && -d "$LEAN_INSTALL_LOCK_DIR" && \
        ! -e "$LEAN_INSTALL_LOCK_DIR/owner" && ! -L "$LEAN_INSTALL_LOCK_DIR/owner" ]] && \
     mv "$recovery_path" "$LEAN_INSTALL_LOCK_DIR/owner"; then
    log "Lean install lock contains unexpected entries; the exact owner record was restored."
  else
    log "Lean install lock release failed; owner recovery record kept at: $recovery_path"
  fi
  return 1
}

release_lean_install_lock() {
  if [[ "$LEAN_INSTALL_LOCK_HELD" != "1" ]]; then
    return
  fi
  if ! remove_lean_install_lock_owned_by "$LEAN_INSTALL_LOCK_TOKEN"; then
    LEAN_INSTALL_LOCK_HELD=0
    return 1
  fi
  LEAN_INSTALL_LOCK_HELD=0
  LEAN_INSTALL_LOCK_TOKEN=""
}

acquire_lean_install_lock() {
  local attempts=0 missing_owner_attempts=0 lock_pid="" lock_token=""

  while ! mkdir -m 700 "$LEAN_INSTALL_LOCK_DIR" 2>/dev/null; do
    if [[ -L "$LEAN_INSTALL_LOCK_DIR" || ! -d "$LEAN_INSTALL_LOCK_DIR" ]]; then
      log "Lean install lock path is not a safe directory: $LEAN_INSTALL_LOCK_DIR"
      exit 1
    fi

    lock_token=""
    if [[ -f "$LEAN_INSTALL_LOCK_DIR/owner" && ! -L "$LEAN_INSTALL_LOCK_DIR/owner" ]]; then
      missing_owner_attempts=0
      IFS= read -r lock_token < "$LEAN_INSTALL_LOCK_DIR/owner" || true
    elif [[ -e "$LEAN_INSTALL_LOCK_DIR/owner" || -L "$LEAN_INSTALL_LOCK_DIR/owner" ]]; then
      log "Lean install lock has an unsafe owner record: $LEAN_INSTALL_LOCK_DIR"
      exit 1
    else
      LEAN_INSTALL_LOCK_WAITED=1
      missing_owner_attempts=$((missing_owner_attempts + 1))
      if [[ "$missing_owner_attempts" -ge 3 ]]; then
        if rmdir "$LEAN_INSTALL_LOCK_DIR" 2>/dev/null; then
          log "Recovered an abandoned empty Lean install lock."
          missing_owner_attempts=0
          continue
        fi
        if [[ ! -f "$LEAN_INSTALL_LOCK_DIR/owner" || -L "$LEAN_INSTALL_LOCK_DIR/owner" ]]; then
          log "Lean install lock stayed uninitialized and contains unexpected entries: $LEAN_INSTALL_LOCK_DIR"
          exit 1
        fi
        missing_owner_attempts=0
        continue
      fi
      attempts=$((attempts + 1))
      if [[ "$((attempts % 30))" -eq 1 ]]; then
        log "Waiting for another MathCode process to initialize the Lean install lock ..."
      fi
      sleep 1
      continue
    fi
    lock_pid="${lock_token%%:*}"
    case "$lock_pid" in
      ''|*[!0-9]*)
        log "Lean install lock has an invalid owner record: $LEAN_INSTALL_LOCK_DIR"
        exit 1
        ;;
      *)
        if ! kill -0 "$lock_pid" 2>/dev/null; then
          if ! remove_lean_install_lock_owned_by "$lock_token"; then
            exit 1
          fi
          continue
        else
          LEAN_INSTALL_LOCK_WAITED=1
        fi
        ;;
    esac

    attempts=$((attempts + 1))
    if [[ "$attempts" -ge 3600 ]]; then
      log "Timed out waiting for another MathCode process to finish installing Lean."
      exit 1
    fi
    if [[ "$((attempts % 30))" -eq 1 ]]; then
      log "Waiting for another MathCode process to finish installing Lean ..."
    fi
    sleep 1
  done

  LEAN_INSTALL_LOCK_TOKEN="$$:${RANDOM}:${RANDOM}"
  if ! printf '%s\n' "$LEAN_INSTALL_LOCK_TOKEN" > "$LEAN_INSTALL_LOCK_DIR/owner"; then
    rmdir "$LEAN_INSTALL_LOCK_DIR" 2>/dev/null || true
    log "Could not record the Lean install lock owner."
    exit 1
  fi
  LEAN_INSTALL_LOCK_HELD=1
  trap 'release_lean_install_lock || true' EXIT
}

mark_lean_deferred() {
  local temp_path
  rm -f "$LEAN_READY_MARKER"
  temp_path="$(mktemp "$ROOT_DIR/.mathcode-lean-deferred.XXXXXX")"
  {
    printf 'release_tag=%s\n' "$RELEASE_TAG"
    printf 'install_command=bash setup.sh --install-lean\n'
  } > "$temp_path"
  chmod 600 "$temp_path"
  mv -f "$temp_path" "$LEAN_DEFERRED_MARKER"
}

mark_lean_ready() {
  local temp_path
  temp_path="$(mktemp "$ROOT_DIR/.mathcode-lean-ready.XXXXXX")"
  {
    printf 'release_tag=%s\n' "$RELEASE_TAG"
  } > "$temp_path"
  chmod 600 "$temp_path"
  mv -f "$temp_path" "$LEAN_READY_MARKER"
  rm -f "$LEAN_DEFERRED_MARKER"
}

install_lean_support() {
  acquire_lean_install_lock
  if [[ "$LEAN_INSTALL_LOCK_WAITED" == "1" ]] && \
     lean_ready_marker_matches_release && lean_runtime_pair_available_for_status; then
    log "Lean/Mathlib installation was completed by another MathCode process."
    release_lean_install_lock
    return
  fi
  mark_lean_deferred
  ensure_lean
  persist_lean_runtime_selection
  ensure_lean_sandbox_dependencies
  bootstrap_lean_workspace
  mark_lean_ready
  release_lean_install_lock
}

show_help() {
  cat <<'EOF'
Usage: bash setup.sh [OPTIONS]

Set up MathCode with or without the optional local Lean/Mathlib runtime.
With no option, an interactive terminal asks which mode to use; a
non-interactive invocation keeps the backward-compatible full install.

Options:
  --with-lean     Install MathCode plus the pinned Lean/Mathlib runtime
  --without-lean  Install MathCode CLI/WebUI only; defer Lean installation
  --install-lean  Add or repair the pinned Lean/Mathlib runtime later
  --help          Show this help message and exit
  --clean         Remove install artifacts (binary, Lean toolchain, .env,
                  caches, and install markers) and exit. User outputs are kept.
  --status        Show what is currently installed and exit

Environment variables:
  MATHCODE_SKIP_MATHLIB_CACHE=1   Skip the ~8 GB Mathlib cache download
  MATHCODE_SETUP_USE_SYSTEM_LEAN=1
                                   Reuse complete system lean/lake instead of local .local/elan
  MATHCODE_INSTALL_BIN_DIR=<dir>  Install the user-local mathcode launcher here
  MATHCODE_SKIP_USER_BIN=1        Skip installing the user-local mathcode launcher

Examples:
  bash setup.sh --with-lean     # full install
  bash setup.sh --without-lean  # lightweight CLI/WebUI install
  bash setup.sh --install-lean  # add Lean/Mathlib later
  bash setup.sh --status        # check installation state
  bash setup.sh --clean         # remove install artifacts, keep proofs/vaults
EOF
}

do_clean() {
  local lock_pid="" lock_token=""
  if [[ -e "$LEAN_INSTALL_LOCK_DIR" || -L "$LEAN_INSTALL_LOCK_DIR" ]]; then
    if [[ -L "$LEAN_INSTALL_LOCK_DIR" || ! -d "$LEAN_INSTALL_LOCK_DIR" ]]; then
      log "Cannot clean an unsafe Lean install lock: $LEAN_INSTALL_LOCK_DIR"
      return 1
    fi
    if [[ ! -e "$LEAN_INSTALL_LOCK_DIR/owner" && ! -L "$LEAN_INSTALL_LOCK_DIR/owner" ]]; then
      if ! rmdir "$LEAN_INSTALL_LOCK_DIR" 2>/dev/null; then
        log "Cannot clean an uninitialized Lean install lock with unexpected entries: $LEAN_INSTALL_LOCK_DIR"
        return 1
      fi
    elif [[ ! -f "$LEAN_INSTALL_LOCK_DIR/owner" || -L "$LEAN_INSTALL_LOCK_DIR/owner" ]]; then
      log "Cannot clean an unsafe Lean install lock owner: $LEAN_INSTALL_LOCK_DIR"
      return 1
    else
    IFS= read -r lock_token < "$LEAN_INSTALL_LOCK_DIR/owner" || true
    lock_pid="${lock_token%%:*}"
    case "$lock_pid" in
      ''|*[!0-9]*)
        log "Cannot clean an invalid Lean install lock: $LEAN_INSTALL_LOCK_DIR"
        return 1
        ;;
      *)
        if kill -0 "$lock_pid" 2>/dev/null; then
          log "Cannot clean while process $lock_pid is installing Lean."
          return 1
        fi
        remove_lean_install_lock_owned_by "$lock_token" || {
          log "Cannot safely recover the stale Lean install lock: $LEAN_INSTALL_LOCK_DIR"
          return 1
        }
        ;;
    esac
    fi
  fi
  log "Cleaning MathCode install artifacts from $ROOT_DIR ..."
  remove_managed_mathcode_command
  rm -rf "$ROOT_DIR/mathcode"
  rm -rf "$ROOT_DIR/mathcode-webui"
  rm -f "$(release_metadata_file)"
  rm -rf "$ROOT_DIR/vendor/ripgrep"
  rmdir "$ROOT_DIR/vendor" 2>/dev/null || true
  rm -rf "$LOCAL_ELAN_HOME"
  rm -f "$ROOT_DIR/.env"
  rm -rf "$LEAN_WORKSPACE_DIR/.lake"
  rm -rf "$LEAN_WORKSPACE_DIR/lake-packages"
  rm -rf "$LEAN_WORKSPACE_DIR/build"
  rm -f "$LEAN_DEFERRED_MARKER" "$LEAN_READY_MARKER"
  log "Kept user outputs in LeanFormalizations/ and ObsidianVault/."
  log "Done. Run 'bash setup.sh' to reinstall."
}

status_line() {
  log "$(printf '%-13s %s' "$1" "$2")"
}

do_status() {
  local binary_version free_kb launcher_path target_path recorded_launcher_path computed_launcher_path

  log "MathCode installation status ($ROOT_DIR)"
  log "──────────────────────────────────────────"

  if [[ -e "$ROOT_DIR/mathcode" ]]; then
    if [[ -x "$ROOT_DIR/mathcode" ]]; then
      if mathcode_binary_matches_release "$ROOT_DIR/mathcode"; then
        binary_version="$("$ROOT_DIR/mathcode" --version 2>/dev/null)"
        status_line "Binary:" "installed ($binary_version)"
      elif mathcode_binary_version_matches_release "$ROOT_DIR/mathcode"; then
        status_line "Binary:" "stale or unverified (expected $RELEASE_TAG)"
      elif binary_version="$("$ROOT_DIR/mathcode" --version 2>/dev/null)"; then
        status_line "Binary:" "stale or wrong version ($binary_version; expected $RELEASE_TAG)"
      else
        status_line "Binary:" "present but broken"
      fi
    else
      status_line "Binary:" "present but broken"
    fi
  else
    status_line "Binary:" "not installed"
  fi

  if [[ -x "$ROOT_DIR/mathcode-webui" ]]; then
    if webui_binary_matches_release "$ROOT_DIR/mathcode-webui"; then
      status_line "WebUI binary:" "installed"
    else
      status_line "WebUI binary:" "stale or unverified (expected $RELEASE_TAG)"
    fi
  elif [[ -e "$ROOT_DIR/mathcode-webui" ]]; then
    status_line "WebUI binary:" "present but broken"
  else
    status_line "WebUI binary:" "not installed"
  fi

  if [[ -x "$ROOT_DIR/run" ]]; then
    status_line "Run wrapper:" "present"
  elif [[ -e "$ROOT_DIR/run" ]]; then
    status_line "Run wrapper:" "present but broken"
  else
    status_line "Run wrapper:" "missing"
  fi

  if bundled_ripgrep_present; then
    status_line "Bundled rg:" "installed ($(bundled_ripgrep_path))"
  elif [[ -e "$(bundled_ripgrep_path)" ]]; then
    status_line "Bundled rg:" "present but broken ($(bundled_ripgrep_path))"
  elif [[ -d "$ROOT_DIR/vendor/ripgrep" ]]; then
    status_line "Bundled rg:" "missing for this platform ($(ripgrep_runtime_dir))"
  else
    status_line "Bundled rg:" "not installed"
  fi

  recorded_launcher_path="$(read_recorded_launcher_path)"
  computed_launcher_path="$(current_launcher_path)"
  target_path="$(current_launcher_target)"
  launcher_path="$recorded_launcher_path"
  if [[ -z "$launcher_path" ]]; then
    launcher_path="$computed_launcher_path"
  fi

  if [[ -n "$launcher_path" && -f "$launcher_path" ]]; then
    if grep -Fq "$MATHCODE_LAUNCHER_MARKER" "$launcher_path" 2>/dev/null; then
      if [[ ! -x "$launcher_path" ]]; then
        status_line "User launcher:" "present but broken ($launcher_path)"
      elif grep -Fq "$target_path" "$launcher_path" 2>/dev/null || \
           grep -Fq "$(shell_quote "$target_path")" "$launcher_path" 2>/dev/null; then
        status_line "User launcher:" "installed ($launcher_path)"
      else
        status_line "User launcher:" "managed launcher points elsewhere ($launcher_path)"
      fi
    else
      status_line "User launcher:" "occupied by another command ($launcher_path)"
    fi
  elif [[ -n "$recorded_launcher_path" ]]; then
    status_line "User launcher:" "missing (expected $recorded_launcher_path)"
  else
    status_line "User launcher:" "not installed"
  fi

  if local_elan_tool_path lean >/dev/null && \
     local_elan_tool_path lake >/dev/null; then
    status_line "Lean:" "installed (local .local/elan)"
  elif local_elan_artifacts_present; then
    status_line "Lean:" "incomplete (repair local .local/elan)"
  elif use_system_lean_requested && \
       { [[ -n "$INITIAL_LEAN_CMD" && -n "$INITIAL_LAKE_CMD" ]] || \
         { have_command lean && have_command lake; }; }; then
    status_line "Lean:" "installed (system, opt-in)"
  elif use_system_lean_requested && \
       { [[ -n "$INITIAL_LEAN_CMD" ]] || [[ -n "$INITIAL_LAKE_CMD" ]] || \
         have_command lean || have_command lake; }; then
    status_line "Lean:" "incomplete system pair (local .local/elan will be installed)"
  elif [[ -f "$LEAN_DEFERRED_MARKER" ]]; then
    status_line "Lean:" "deferred (run bash setup.sh --install-lean)"
  else
    status_line "Lean:" "not installed (local .local/elan will be installed)"
  fi

  if lean_ready_marker_matches_release && lean_runtime_pair_available_for_status; then
    status_line "Lean workspace:" "ready"
  elif lean_ready_marker_matches_release; then
    status_line "Lean workspace:" "incomplete (run bash setup.sh --install-lean)"
  elif [[ -e "$LEAN_READY_MARKER" || -L "$LEAN_READY_MARKER" ]]; then
    status_line "Lean workspace:" "stale or unsafe (run bash setup.sh --install-lean)"
  elif [[ -f "$LEAN_DEFERRED_MARKER" ]]; then
    status_line "Lean workspace:" "deferred"
  else
    status_line "Lean workspace:" "not prepared"
  fi

  if [[ -f "$ROOT_DIR/.env" ]]; then
    status_line ".env:" "present"
  else
    status_line ".env:" "not created"
  fi

  free_kb="$(available_kb "$ROOT_DIR")"
  if [[ -n "$free_kb" ]]; then
    status_line "Disk free:" "$(( free_kb / 1024 )) MB"
  fi
}

normalize_os() {
  case "$(uname -s)" in
    Darwin) printf 'darwin\n' ;;
    Linux) printf 'linux\n' ;;
    *)
      log "Unsupported operating system for MathCode releases: $(uname -s)"
      exit 1
      ;;
  esac
}

normalize_arch() {
  case "$(uname -m)" in
    arm64|aarch64) printf 'arm64\n' ;;
    x86_64|amd64) printf 'x86_64\n' ;;
    *)
      log "Unsupported CPU architecture for MathCode releases: $(uname -m)"
      exit 1
      ;;
  esac
}

release_archive_name() {
  printf 'mathcode-%s-%s-%s.tar.gz\n' "$RELEASE_TAG" "$(normalize_os)" "$(normalize_arch)"
}

release_bundle_name() {
  printf 'mathcode-%s-%s-%s\n' "$RELEASE_TAG" "$(normalize_os)" "$(normalize_arch)"
}

ripgrep_runtime_arch() {
  case "$(uname -m)" in
    arm64|aarch64) printf 'arm64\n' ;;
    x86_64|amd64) printf 'x64\n' ;;
    *)
      log "Unsupported CPU architecture for bundled ripgrep: $(uname -m)"
      exit 1
      ;;
  esac
}

ripgrep_runtime_dir() {
  printf '%s-%s\n' "$(ripgrep_runtime_arch)" "$(normalize_os)"
}

ripgrep_binary_name() {
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) printf 'rg.exe\n' ;;
    *) printf 'rg\n' ;;
  esac
}

bundled_ripgrep_path() {
  printf '%s/vendor/ripgrep/%s/%s\n' "$ROOT_DIR" "$(ripgrep_runtime_dir)" "$(ripgrep_binary_name)"
}

ripgrep_binary_works() {
  local binary_path="$1"
  local version_output
  [[ -f "$binary_path" && -x "$binary_path" ]] || return 1
  version_output="$("$binary_path" --version 2>/dev/null | head -n 1)" || return 1
  case "$version_output" in
    *ripgrep*|*Ripgrep*) return 0 ;;
    *) return 1 ;;
  esac
}

bundled_ripgrep_present() {
  ripgrep_binary_works "$(bundled_ripgrep_path)"
}

select_release_asset_source() {
  local archive_name="$1"
  local local_archive="$LOCAL_RELEASE_ASSET_DIR/$archive_name"
  local local_checksums="$LOCAL_RELEASE_ASSET_DIR/SHA256SUMS.txt"
  local archive_present=0 checksums_present=0

  USE_LOCAL_RELEASE_ASSETS=0
  if [[ ! -e "$LOCAL_RELEASE_ASSET_DIR" && ! -L "$LOCAL_RELEASE_ASSET_DIR" ]]; then
    return
  fi
  if [[ -L "$LOCAL_RELEASE_ASSET_DIR" || ! -d "$LOCAL_RELEASE_ASSET_DIR" ]]; then
    log "Local release asset path must be a real directory: $LOCAL_RELEASE_ASSET_DIR"
    return 1
  fi
  [[ -f "$local_archive" && ! -L "$local_archive" ]] && archive_present=1
  [[ -f "$local_checksums" && ! -L "$local_checksums" ]] && checksums_present=1
  if [[ "$archive_present" == "1" && "$checksums_present" == "1" ]]; then
    USE_LOCAL_RELEASE_ASSETS=1
    return
  fi
  if [[ "$archive_present" != "$checksums_present" ]]; then
    log "Local release assets are incomplete; require both $local_archive and $local_checksums."
    return 1
  fi
}

download_release_file() {
  local filename="$1"
  local output_path="$2"
  local url="https://github.com/$RELEASE_REPO/releases/download/$RELEASE_TAG/$filename"
  local local_path="$LOCAL_RELEASE_ASSET_DIR/$filename"

  if [[ "$USE_LOCAL_RELEASE_ASSETS" == "1" ]]; then
    if ! cp "$local_path" "$output_path"; then
      log "Failed to copy local release asset: $local_path"
      return 1
    fi
    log "Using local release asset: $local_path"
    return 0
  fi

  ensure_curl

  if ! "$CURL_CMD" -fL --retry 3 --retry-delay 1 -o "$output_path" "$url"; then
    log "Failed to download $url"
    log ""
    log "Check network connectivity and confirm the asset exists at:"
    log "  https://github.com/$RELEASE_REPO/releases/tag/$RELEASE_TAG"
    return 1
  fi
}

verify_release_archive() {
  local temp_dir="$1"
  local archive_name="$2"
  local checksum_line

  if ! checksum_line="$(awk -v archive_name="$archive_name" '
    $2 == archive_name {
      print
      found = 1
      exit
    }
    END {
      if (!found) exit 1
    }
  ' "$temp_dir/SHA256SUMS.txt")"; then
    log "Release checksum file did not include $archive_name."
    return 1
  fi

  if have_command shasum; then
    (
      cd "$temp_dir"
      printf '%s\n' "$checksum_line" | LC_ALL=C LANG=C shasum -a 256 -c -
    )
    return $?
  fi

  if have_command sha256sum; then
    (
      cd "$temp_dir"
      printf '%s\n' "$checksum_line" | sha256sum -c -
    )
    return $?
  fi

  log "Cannot verify release archive: shasum or sha256sum is required."
  return 1
}

normalize_version_tag() {
  local version="$1"
  version="${version%%$'\n'*}"
  version="${version%% *}"
  version="${version#v}"
  [[ -n "$version" ]] || return 1
  printf 'v%s\n' "$version"
}

release_metadata_file() {
  printf '%s/.mathcode-release\n' "$ROOT_DIR"
}

release_metadata_value() {
  local key="$1"
  local file
  file="$(release_metadata_file)"
  [[ -f "$file" ]] || return 1
  awk -F= -v key="$key" '
    $1 == key {
      print substr($0, length($1) + 2)
      found = 1
      exit
    }
    END {
      if (!found) exit 1
    }
  ' "$file"
}

have_release_checksum_tool() {
  have_command shasum || have_command sha256sum
}

file_sha256() {
  local file="$1"

  if have_command shasum; then
    LC_ALL=C LANG=C shasum -a 256 "$file" | awk '{ print $1 }'
    return
  fi

  if have_command sha256sum; then
    sha256sum "$file" | awk '{ print $1 }'
    return
  fi

  printf 'Need `shasum` or `sha256sum` to compute MathCode release metadata.\n' >&2
  return 1
}

write_release_metadata() {
  local binary_path="$1"
  local webui_binary_path="$2"
  local metadata_path temp_path binary_sha webui_sha
  binary_sha="$(file_sha256 "$binary_path")" || return 1
  webui_sha="$(file_sha256 "$webui_binary_path")" || return 1
  metadata_path="$(release_metadata_file)"
  temp_path="$(mktemp "$ROOT_DIR/.mathcode-release.XXXXXX")" || return 1
  {
    printf 'release_tag=%s\n' "$RELEASE_TAG"
    printf 'runtime_generation=%s\n' "$RELEASE_RUNTIME_GENERATION"
    printf 'mathcode_sha256=%s\n' "$binary_sha"
    printf 'mathcode_webui_sha256=%s\n' "$webui_sha"
  } >"$temp_path" || {
    rm -f "$temp_path"
    return 1
  }
  mv "$temp_path" "$metadata_path"
}

backup_artifact() {
  local target_path="$1"
  local backup_path="$2"

  if [[ -e "$target_path" || -L "$target_path" ]]; then
    mv "$target_path" "$backup_path"
  fi
}

restore_artifact_backup() {
  local target_path="$1"
  local backup_path="$2"

  rm -rf "$target_path" 2>/dev/null || true
  if [[ -e "$backup_path" || -L "$backup_path" ]]; then
    mv "$backup_path" "$target_path"
  fi
}

restore_existing_artifact_backup() {
  local target_path="$1"
  local backup_path="$2"

  if [[ -e "$backup_path" || -L "$backup_path" ]]; then
    rm -rf "$target_path" 2>/dev/null || true
    mv "$backup_path" "$target_path"
  fi
}

mathcode_binary_version_matches_release() {
  local binary_path="$1"
  local binary_version expected_version actual_version
  binary_version="$("$binary_path" --version 2>/dev/null)" || return 1
  expected_version="$(normalize_version_tag "$RELEASE_TAG")" || return 1
  actual_version="$(normalize_version_tag "$binary_version")" || return 1
  [[ "$actual_version" == "$expected_version" ]]
}

release_metadata_generation_matches() {
  local recorded_generation
  recorded_generation="$(release_metadata_value runtime_generation)" || return 1
  [[ "$recorded_generation" == "$RELEASE_RUNTIME_GENERATION" ]]
}

mathcode_binary_matches_release() {
  local binary_path="$1"
  local recorded_tag expected_version actual_version recorded_sha actual_sha
  mathcode_binary_version_matches_release "$binary_path" || return 1
  release_metadata_generation_matches || return 1
  recorded_tag="$(release_metadata_value release_tag)" || return 1
  expected_version="$(normalize_version_tag "$RELEASE_TAG")" || return 1
  actual_version="$(normalize_version_tag "$recorded_tag")" || return 1
  [[ "$actual_version" == "$expected_version" ]] || return 1
  recorded_sha="$(release_metadata_value mathcode_sha256)" || return 1
  [[ "$recorded_sha" =~ ^[0-9a-f]{64}$ ]] || return 1
  actual_sha="$(file_sha256 "$binary_path")" || return 1
  [[ "$actual_sha" == "$recorded_sha" ]]
}

webui_binary_matches_release() {
  local webui_binary_path="$1"
  local recorded_tag expected_version actual_version recorded_sha actual_sha
  [[ -x "$webui_binary_path" ]] || return 1
  release_metadata_generation_matches || return 1
  recorded_tag="$(release_metadata_value release_tag)" || return 1
  expected_version="$(normalize_version_tag "$RELEASE_TAG")" || return 1
  actual_version="$(normalize_version_tag "$recorded_tag")" || return 1
  [[ "$actual_version" == "$expected_version" ]] || return 1
  recorded_sha="$(release_metadata_value mathcode_webui_sha256)" || return 1
  [[ "$recorded_sha" =~ ^[0-9a-f]{64}$ ]] || return 1
  actual_sha="$(file_sha256 "$webui_binary_path")" || return 1
  [[ "$actual_sha" == "$recorded_sha" ]]
}

ensure_mathcode_binary() {
  local binary_path="$ROOT_DIR/mathcode"
  local webui_binary_path="$ROOT_DIR/mathcode-webui"
  local metadata_path
  local archive_name bundle_name temp_dir archive_path checksum_path
  local extract_dir staged_binary_path staged_webui_binary_path staged_vendor_dir
  local temp_binary_path temp_webui_binary_path temp_vendor_dir backup_root
  local binary_backup webui_backup vendor_backup metadata_backup

  if [[ -x "$binary_path" ]] \
    && mathcode_binary_matches_release "$binary_path" \
    && webui_binary_matches_release "$webui_binary_path" \
    && bundled_ripgrep_present; then
    return
  fi

  if ! have_release_checksum_tool; then
    printf 'Need `shasum` or `sha256sum` to compute MathCode release metadata.\n' >&2
    exit 1
  fi

  archive_name="$(release_archive_name)"
  bundle_name="$(release_bundle_name)"
  if ! select_release_asset_source "$archive_name"; then
    exit 1
  fi
  temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/mathcode-bootstrap.XXXXXX")"
  archive_path="$temp_dir/$archive_name"
  checksum_path="$temp_dir/SHA256SUMS.txt"

  if [[ "$USE_LOCAL_RELEASE_ASSETS" == "1" ]]; then
    log "Restoring MathCode from local release assets ($RELEASE_TAG, $(normalize_os)/$(normalize_arch))"
  else
    log "Downloading MathCode release from GitHub Releases ($RELEASE_TAG, $(normalize_os)/$(normalize_arch))"
  fi
  if ! download_release_file "$archive_name" "$archive_path"; then
    rm -rf "$temp_dir"
    exit 1
  fi
  if ! download_release_file "SHA256SUMS.txt" "$checksum_path"; then
    rm -rf "$temp_dir"
    exit 1
  fi
  if ! verify_release_archive "$temp_dir" "$archive_name"; then
    rm -rf "$temp_dir"
    exit 1
  fi

  extract_dir="$temp_dir/extract"
  mkdir -p "$extract_dir"
  if ! LC_ALL=C LANG=C tar -xzf "$archive_path" \
    -C "$extract_dir" \
    "$bundle_name/mathcode" \
    "$bundle_name/mathcode-webui" \
    "$bundle_name/vendor"; then
    rm -rf "$temp_dir"
    log "Downloaded release archive could not be extracted."
    exit 1
  fi

  staged_binary_path="$extract_dir/$bundle_name/mathcode"
  staged_webui_binary_path="$extract_dir/$bundle_name/mathcode-webui"
  staged_vendor_dir="$extract_dir/$bundle_name/vendor"
  chmod +x "$staged_binary_path"
  chmod +x "$staged_webui_binary_path"
  if [[ ! -f "$staged_vendor_dir/ripgrep/$(ripgrep_runtime_dir)/$(ripgrep_binary_name)" ]]; then
    rm -rf "$temp_dir"
    log "Downloaded release archive did not include bundled ripgrep."
    exit 1
  fi
  chmod +x "$staged_vendor_dir/ripgrep/$(ripgrep_runtime_dir)/$(ripgrep_binary_name)"

  if ! mathcode_binary_version_matches_release "$staged_binary_path"; then
    rm -rf "$temp_dir"
    log "Downloaded MathCode binary did not start successfully or does not match $RELEASE_TAG."
    exit 1
  fi
  if [[ ! -s "$staged_webui_binary_path" || ! -x "$staged_webui_binary_path" ]]; then
    rm -rf "$temp_dir"
    log "Downloaded MathCode WebUI binary was not executable."
    exit 1
  fi
  if ! ripgrep_binary_works "$staged_vendor_dir/ripgrep/$(ripgrep_runtime_dir)/$(ripgrep_binary_name)"; then
    rm -rf "$temp_dir"
    log "Downloaded bundled ripgrep did not start successfully."
    exit 1
  fi

  temp_binary_path="$(mktemp "$ROOT_DIR/.mathcode-bin.XXXXXX")"
  temp_webui_binary_path="$(mktemp "$ROOT_DIR/.mathcode-webui.XXXXXX")"
  temp_vendor_dir="$(mktemp -d "$ROOT_DIR/.vendor.XXXXXX")"
  backup_root="$(mktemp -d "$ROOT_DIR/.artifact-backups.XXXXXX")"
  binary_backup="$backup_root/mathcode"
  webui_backup="$backup_root/mathcode-webui"
  vendor_backup="$backup_root/vendor"
  metadata_path="$(release_metadata_file)"
  metadata_backup="$backup_root/.mathcode-release"

  cp "$staged_binary_path" "$temp_binary_path"
  cp "$staged_webui_binary_path" "$temp_webui_binary_path"
  cp -R "$staged_vendor_dir/." "$temp_vendor_dir/"
  chmod 755 "$temp_binary_path" "$temp_webui_binary_path"
  chmod -R u+rwX,go+rX "$temp_vendor_dir"

  if ! backup_artifact "$binary_path" "$binary_backup" || \
     ! backup_artifact "$webui_binary_path" "$webui_backup" || \
     ! backup_artifact "$ROOT_DIR/vendor" "$vendor_backup" || \
     ! backup_artifact "$metadata_path" "$metadata_backup"; then
    restore_existing_artifact_backup "$binary_path" "$binary_backup" || true
    restore_existing_artifact_backup "$webui_binary_path" "$webui_backup" || true
    restore_existing_artifact_backup "$ROOT_DIR/vendor" "$vendor_backup" || true
    restore_existing_artifact_backup "$metadata_path" "$metadata_backup" || true
    rm -rf "$temp_dir" "$temp_binary_path" "$temp_webui_binary_path" "$temp_vendor_dir" "$backup_root"
    log "Downloaded release archive could not stage backups for the current runtime."
    exit 1
  fi

  if ! mv "$temp_binary_path" "$binary_path" || \
     ! mv "$temp_webui_binary_path" "$webui_binary_path" || \
     ! mv "$temp_vendor_dir" "$ROOT_DIR/vendor" || \
     ! write_release_metadata "$binary_path" "$webui_binary_path"; then
    restore_artifact_backup "$binary_path" "$binary_backup" || true
    restore_artifact_backup "$webui_binary_path" "$webui_backup" || true
    restore_artifact_backup "$ROOT_DIR/vendor" "$vendor_backup" || true
    restore_artifact_backup "$metadata_path" "$metadata_backup" || true
    rm -rf "$temp_dir" "$temp_binary_path" "$temp_webui_binary_path" "$temp_vendor_dir" "$backup_root"
    log "Downloaded release archive could not replace the current runtime atomically."
    exit 1
  fi
  rm -rf "$backup_root"

  rm -rf "$temp_dir"
  log "Installed MathCode binaries"
}

ensure_env_file() {
  local quoted_lean_project_dir

  if [[ -f "$ROOT_DIR/.env" ]]; then
    return
  fi

  quoted_lean_project_dir="$(env_file_quote "$ROOT_DIR/lean-workspace")"

  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"

  cat >> "$ROOT_DIR/.env" <<PATHS

# Binary distribution paths (auto-generated by setup.sh)
LEAN_PROJECT_DIR=$quoted_lean_project_dir

# Lean server (persistent REPL for sub-second compile checks)
MATHCODE_LEAN_REPL=1
PATHS

  log "Created .env; the Lean server will be enabled when Lean support is available."
}

install_local_lean() {
  if [[ ! -f "$LEAN_WORKSPACE_DIR/lean-toolchain" ]]; then
    log "Bundled Lean workspace is missing: $LEAN_WORKSPACE_DIR"
    exit 1
  fi

  local lean_toolchain
  lean_toolchain="$(<"$LEAN_WORKSPACE_DIR/lean-toolchain")"

  case "$OSTYPE" in
    darwin*|linux*)
      log "Installing Lean locally into .local/elan"
      rm -rf "$LOCAL_ELAN_HOME"
      ensure_curl
      "$CURL_CMD" -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | env ELAN_HOME="$LOCAL_ELAN_HOME" sh -s -- -y --default-toolchain "$lean_toolchain" --no-modify-path
      ;;
    *)
      log "Lean is not installed and this script only auto-installs Lean locally on macOS/Linux."
      log "Install elan manually, then rerun ./setup.sh."
      exit 1
      ;;
  esac
}

use_local_lean_paths() {
  unset ELAN_TOOLCHAIN
  export ELAN_HOME="$LOCAL_ELAN_HOME"
  prepend_to_path "$LOCAL_ELAN_BIN"
}

select_concrete_system_lean_paths() {
  local system_lean="$1"
  local system_lake="$2"
  local lean_toolchain expected_version lean_prefix candidate_lean candidate_lake
  local lean_version lake_version_output lake_lean_version

  [[ -f "$system_lean" && -x "$system_lean" &&
     -f "$system_lake" && -x "$system_lake" ]] || return 1
  [[ -f "$LEAN_WORKSPACE_DIR/lean-toolchain" ]] || return 1
  lean_toolchain="$(<"$LEAN_WORKSPACE_DIR/lean-toolchain")"
  lean_toolchain="${lean_toolchain%$'\r'}"
  if [[ -z "$lean_toolchain" || "$lean_toolchain" == *$'\n'* ||
        "$lean_toolchain" == *$'\r'* || "$lean_toolchain" != *:* ]]; then
    return 1
  fi
  expected_version="${lean_toolchain##*:}"
  expected_version="${expected_version#v}"
  case "$expected_version" in
    ''|*[!0-9A-Za-z_.-]*) return 1 ;;
  esac

  if ! lean_prefix="$(
    cd "$LEAN_WORKSPACE_DIR"
    unset ELAN_TOOLCHAIN
    "$system_lean" --print-prefix 2>/dev/null
  )"; then
    return 1
  fi
  lean_prefix="${lean_prefix%$'\r'}"
  if [[ -z "$lean_prefix" || "$lean_prefix" != /* ||
        "$lean_prefix" == *$'\n'* || "$lean_prefix" == *$'\r'* ]]; then
    return 1
  fi

  candidate_lean="$lean_prefix/bin/lean"
  candidate_lake="$lean_prefix/bin/lake"
  if [[ ! -f "$candidate_lean" || ! -x "$candidate_lean" ||
        ! -f "$candidate_lake" || ! -x "$candidate_lake" ]]; then
    return 1
  fi

  if ! lean_version="$(unset ELAN_TOOLCHAIN; "$candidate_lean" --short-version 2>/dev/null)"; then
    return 1
  fi
  lean_version="${lean_version#v}"
  lean_version="${lean_version%$'\r'}"
  [[ "$lean_version" == "$expected_version" ]] || return 1

  if ! lake_version_output="$(unset ELAN_TOOLCHAIN; "$candidate_lake" --version 2>&1)"; then
    return 1
  fi
  lake_lean_version="$(printf '%s\n' "$lake_version_output" | sed -nE \
    's/^.*\(Lean version v?([0-9A-Za-z_.-]+)\).*$/\1/p')"
  [[ "$lake_lean_version" == "$expected_version" ]] || return 1

  LEAN_CMD="$(resolve_command_path "$candidate_lean")"
  LAKE_CMD="$(resolve_command_path "$candidate_lake")"
  return 0
}

use_existing_system_lean_if_available() {
  local system_lean system_lake

  if [[ -n "$INITIAL_LEAN_CMD" && -n "$INITIAL_LAKE_CMD" ]]; then
    system_lean="$INITIAL_LEAN_CMD"
    system_lake="$INITIAL_LAKE_CMD"
  elif have_command lean && have_command lake; then
    system_lean="$(resolve_command_path "$(command -v lean)")"
    system_lake="$(resolve_command_path "$(command -v lake)")"
  else
    return 1
  fi

  if ! select_concrete_system_lean_paths "$system_lean" "$system_lake"; then
    log "System Lean/Lake did not resolve to the pinned bundled toolchain; using local Lean instead."
    return 1
  fi
  USING_SYSTEM_LEAN=1
  log "Using existing Lean: $LEAN_CMD"
  log "Using existing Lake: $LAKE_CMD"
  return 0
}

persist_lean_runtime_selection() {
  local temp_path encoded_lean_path encoded_lake_path

  if ! temp_path="$(mktemp "$ROOT_DIR/.env.mathcode.XXXXXX")"; then
    log "Could not create a temporary .env file for the Lean runtime selection."
    exit 1
  fi
  if ! awk '
    !/^MATHCODE_SETUP_USE_SYSTEM_LEAN=/ &&
    !/^MATHCODE_SYSTEM_LEAN_PATH=/ &&
    !/^MATHCODE_SYSTEM_LAKE_PATH=/ { print }
  ' "$ROOT_DIR/.env" > "$temp_path"; then
    rm -f "$temp_path"
    log "Could not preserve .env while recording the Lean runtime selection."
    exit 1
  fi
  if [[ "$USING_SYSTEM_LEAN" == "1" ]]; then
    if [[ "$LEAN_CMD" == *$'\n'* || "$LEAN_CMD" == *$'\r'* ||
          "$LAKE_CMD" == *$'\n'* || "$LAKE_CMD" == *$'\r'* ]]; then
      rm -f "$temp_path"
      log "System Lean/Lake paths may not contain line breaks."
      exit 1
    fi
    if ! encoded_lean_path="$(encode_env_file_path "$LEAN_CMD")"; then
      rm -f "$temp_path"
      log "Could not encode the system Lean path for .env."
      exit 1
    fi
    if ! encoded_lake_path="$(encode_env_file_path "$LAKE_CMD")"; then
      rm -f "$temp_path"
      log "Could not encode the system Lake path for .env."
      exit 1
    fi
    printf '\n%s\n%s\n%s\n' \
      'MATHCODE_SETUP_USE_SYSTEM_LEAN=1' \
      "MATHCODE_SYSTEM_LEAN_PATH=$encoded_lean_path" \
      "MATHCODE_SYSTEM_LAKE_PATH=$encoded_lake_path" >> "$temp_path"
  fi
  chmod 600 "$temp_path"
  if ! mv -f "$temp_path" "$ROOT_DIR/.env"; then
    rm -f "$temp_path"
    log "Could not record the Lean runtime selection in .env."
    exit 1
  fi
}

ensure_lean() {
  local local_lean_cmd local_lake_cmd

  if local_lean_cmd="$(local_elan_tool_path lean)" && local_lake_cmd="$(local_elan_tool_path lake)"; then
    if use_system_lean_requested && use_existing_system_lean_if_available; then
      return
    fi
    use_local_lean_paths
    LEAN_CMD="$local_lean_cmd"
    LAKE_CMD="$local_lake_cmd"
    log "Using local Lean: $LEAN_CMD"
    log "Using local Lake: $LAKE_CMD"
    return
  fi

  # Release launchers put bundle-local .local/elan first at runtime. If that
  # tree exists but is incomplete, repair it before considering system Lean so
  # setup does not succeed with tools the launcher will later shadow.
  if local_elan_artifacts_present; then
    install_local_lean
    use_local_lean_paths
    if ! have_command lean || ! have_command lake; then
      log "Lean installation did not expose lean and lake on PATH."
      exit 1
    fi
    LEAN_CMD="$(resolve_command_path "$(command -v lean)")"
    LAKE_CMD="$(resolve_command_path "$(command -v lake)")"
    log "Using local Lean: $LEAN_CMD"
    log "Using local Lake: $LAKE_CMD"
    return
  fi

  if use_system_lean_requested && use_existing_system_lean_if_available; then
    return
  fi

  # No complete Lean/Lake pair was selected. Install locally by default; this
  # also handles an opt-in system pair where only one command was available.
  install_local_lean
  use_local_lean_paths

  if ! have_command lean || ! have_command lake; then
    if [[ ! -f "$LEAN_WORKSPACE_DIR/lean-toolchain" ]]; then
      log "Bundled Lean workspace is missing: $LEAN_WORKSPACE_DIR"
      exit 1
    fi
    log "Lean installation did not expose lean and lake on PATH."
    exit 1
  fi

  LEAN_CMD="$(resolve_command_path "$(command -v lean)")"
  LAKE_CMD="$(resolve_command_path "$(command -v lake)")"
  log "Using local Lean: $LEAN_CMD"
  log "Using local Lake: $LAKE_CMD"
}

ensure_lean_sandbox_dependencies() {
  case "$OSTYPE" in
    linux*)
      local missing=()
      have_command bwrap || missing+=("bwrap (package: bubblewrap)")
      have_command socat || missing+=("socat (package: socat)")
      if [[ "${#missing[@]}" -gt 0 ]]; then
        log "Linux Lean sandbox prerequisites are missing: ${missing[*]}"
        log "Install them (for Debian/Ubuntu: sudo apt-get install bubblewrap socat), then rerun setup."
        exit 1
      fi
      if ! bwrap --new-session --die-with-parent --unshare-net \
        --ro-bind / / --dev /dev --unshare-pid --proc /proc -- \
        /bin/true >/dev/null 2>&1; then
        log "Linux bubblewrap is installed but cannot create the network/PID sandbox required by Lean."
        if [[ -r /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]] &&
          [[ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns)" == "1" ]]; then
          log "Ubuntu AppArmor currently blocks unprivileged user namespaces."
          log "Ask an administrator to enable them (for example: sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0), then rerun setup."
        else
          log "Enable unprivileged user namespaces for bubblewrap, then rerun setup."
        fi
        exit 1
      fi
      ;;
  esac
}

bootstrap_lean_workspace() {
  local skip_cache=0
  local free_kb
  local cache_status
  local vault_libs_dir
  local user_vault_libs_dir
  local managed_dir

  if [[ -L "$LEAN_WORKSPACE_DIR" ]] || [[ ! -d "$LEAN_WORKSPACE_DIR" ]]; then
    log "Managed Lean workspace must be a real directory: $LEAN_WORKSPACE_DIR"
    exit 1
  fi
  if [[ ! -f "$LEAN_WORKSPACE_DIR/lakefile.toml" ]]; then
    log "Bundled Lean workspace is missing: $LEAN_WORKSPACE_DIR"
    exit 1
  fi
  if [[ ! -f "$LEAN_WORKSPACE_DIR/lake-manifest.json" ]]; then
    log "Bundled Lean workspace lockfile is missing: $LEAN_WORKSPACE_DIR/lake-manifest.json"
    exit 1
  fi

  vault_libs_dir="$LEAN_WORKSPACE_DIR/VaultLibs"
  user_vault_libs_dir="$vault_libs_dir/UserVaultLibs"
  for managed_dir in "$vault_libs_dir" "$user_vault_libs_dir"; do
    if [[ -L "$managed_dir" ]] ||
       { [[ -e "$managed_dir" ]] && [[ ! -d "$managed_dir" ]]; }; then
      log "Managed Lean vault directory must be a real directory: $managed_dir"
      exit 1
    fi
    if [[ ! -d "$managed_dir" ]]; then
      mkdir "$managed_dir"
    fi
    if [[ -L "$managed_dir" ]] || [[ ! -d "$managed_dir" ]]; then
      log "Could not safely create managed Lean vault directory: $managed_dir"
      exit 1
    fi
  done

  if [[ "${MATHCODE_SKIP_MATHLIB_CACHE:-0}" == "1" ]]; then
    skip_cache=1
    log "Skipping Mathlib cache download because MATHCODE_SKIP_MATHLIB_CACHE=1."
  else
    free_kb="$(available_kb "$LEAN_WORKSPACE_DIR")"
    if [[ -n "$free_kb" && "$free_kb" -lt "$MATHLIB_CACHE_MIN_KB" ]]; then
      skip_cache=1
      log "Skipping 'lake exe cache get' because disk space is low (${free_kb}KB free)."
      log "Free up more disk space and rerun setup later if you want the bundled Mathlib cache."
    fi
  fi

  log "Bootstrapping bundled Lean workspace"
  (
    cd "$LEAN_WORKSPACE_DIR"
    "$LAKE_CMD" env "$LEAN_CMD" --version >/dev/null
  )

  if [[ "$skip_cache" -eq 0 ]]; then
    log "Fetching Mathlib cache (best effort)"
    set +e
    (
      cd "$LEAN_WORKSPACE_DIR"
      "$LAKE_CMD" exe cache get
    )
    cache_status=$?
    set -e

    if [[ "$cache_status" -ne 0 ]]; then
      log "Warning: 'lake exe cache get' failed. Falling back to a local Lean workspace build."
    fi
  fi

  # Library publication later runs with dependency checkouts read-only. Build
  # the checked-in root while setup owns the cache so Lake can materialize any
  # host-local hash sidecars omitted by distributed artifacts.
  log "Building bundled Lean workspace"
  (
    cd "$LEAN_WORKSPACE_DIR"
    "$LAKE_CMD" --old --no-cache build MathCodeLean
  )
}

SETUP_MODE=""
for arg in "$@"; do
  case "$arg" in
    --help|-h)
      show_help
      exit 0
      ;;
    --clean)
      do_clean
      exit 0
      ;;
    --status)
      do_status
      exit 0
      ;;
    --with-lean)
      if [[ -n "$SETUP_MODE" ]]; then
        log "Choose exactly one install mode."
        exit 1
      fi
      SETUP_MODE="with-lean"
      ;;
    --without-lean)
      if [[ -n "$SETUP_MODE" ]]; then
        log "Choose exactly one install mode."
        exit 1
      fi
      SETUP_MODE="without-lean"
      ;;
    --install-lean)
      if [[ -n "$SETUP_MODE" ]]; then
        log "Choose exactly one install mode."
        exit 1
      fi
      SETUP_MODE="install-lean"
      ;;
    *)
      log "Unknown option: $arg"
      log "Run 'bash setup.sh --help' for usage."
      exit 1
      ;;
  esac
done

if [[ -z "$SETUP_MODE" ]]; then
  if [[ -t 0 && -t 1 ]]; then
    log "Choose an installation mode:"
    log "  1) MathCode with Lean/Mathlib (full install, default)"
    log "  2) MathCode without Lean/Mathlib (install it on first local Lean use)"
    printf 'Selection [1/2]: '
    IFS= read -r setup_selection || setup_selection=""
    case "$setup_selection" in
      ''|1) SETUP_MODE="with-lean" ;;
      2) SETUP_MODE="without-lean" ;;
      *)
        log "Invalid selection: $setup_selection"
        exit 1
        ;;
    esac
  else
    SETUP_MODE="with-lean"
  fi
fi

ensure_mathcode_binary
ensure_env_file
install_mathcode_command

case "$SETUP_MODE" in
  without-lean)
    if lean_ready_marker_matches_release; then
      log "Lean/Mathlib is already ready; --without-lean does not remove an existing installation."
    else
      mark_lean_deferred
      log "Lean/Mathlib installation was deferred."
      log "Run 'bash setup.sh --install-lean' later, or approve a local Lean tool call to install it automatically."
    fi
    ;;
  with-lean|install-lean)
    install_lean_support
    ;;
esac

if [[ "$SETUP_MODE" == "install-lean" ]]; then
  log "Lean/Mathlib installation complete."
else
  log "Release setup complete."
fi
if [[ "$MATHCODE_COMMAND_AVAILABLE_NOW" == "1" ]]; then
  log "Run: mathcode"
elif [[ "$MATHCODE_USER_LAUNCHER_INSTALLED" == "1" ]]; then
  log "Run now: ./run"
  if [[ "$MATHCODE_COMMAND_READY_AFTER_RELOAD" == "1" ]]; then
    log "After reloading your shell, you can also run: mathcode"
  else
    log "The user-local launcher was installed, but your shell profile was not updated."
    log "Add the launcher directory to PATH manually to run: mathcode"
  fi
else
  log "Run: ./run"
fi
