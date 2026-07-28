# claude-playbook: project-scoped command logging (zsh)
# Sourced from ~/.zshenv — logs non-interactive commands to the current user's
# lane: .agent/bash_history, or .agent/<user>/bash_history.
# Purpose: forensic post-mortem record ("what did the agent actually run?")

# Only log non-interactive shells (agent invocations, not user terminals)
if [[ $- == *i* || -z "$ZSH_EXECUTION_STRING" ]]; then
    return 0 2>/dev/null || true
fi

# Walk up from $PWD looking for .agent/ directory
_cpb_log_dir="$PWD"
while [[ "$_cpb_log_dir" != "/" ]]; do
    if [[ -d "$_cpb_log_dir/.agent" ]]; then
        # Extract actual command from CC's eval wrapper:
        #   ... && eval 'CMD' ... && pwd -P >| TMPFILE   (single quotes)
        #   ... && eval "CMD" ... && pwd -P >| TMPFILE   (double quotes, when CMD has single quotes)
        # Anchor on '&& pwd -P >|' (always present, never in user commands)
        if [[ "$ZSH_EXECUTION_STRING" == *"eval '"*"&& pwd -P >|"* ]]; then
            _cpb_log_cmd="${ZSH_EXECUTION_STRING#*eval \'}"
            _cpb_log_cmd="${_cpb_log_cmd%\' *&& pwd -P >|*}"
        elif [[ "$ZSH_EXECUTION_STRING" == *'eval "'*"&& pwd -P >|"* ]]; then
            _cpb_log_cmd="${ZSH_EXECUTION_STRING#*eval \"}"
            _cpb_log_cmd="${_cpb_log_cmd%\" *&& pwd -P >|*}"
        else
            _cpb_log_cmd="$ZSH_EXECUTION_STRING"
        fi
        _cpb_log_cmd="${_cpb_log_cmd//$'\n'/\\n}"

        # Log into THIS user's lane — the same file the tasks CLI reads
        # (resolve_agent_dir()/bash_history). Root-only writes are invisible to
        # retro/context on a multi-user repo and mix users' commands together.
        # An unusable marker means the owning lane is unknown: skip logging
        # rather than fall back to the shared root.
        _cpb_log_lane="$_cpb_log_dir/.agent"
        if [[ -f "$_cpb_log_dir/.agent/current_user" ]]; then
            _cpb_log_user=""; _cpb_log_extra=""
            # One-line contract — see bash-log.sh for the rationale.
            { read -r _cpb_log_user; read -r _cpb_log_extra; } < "$_cpb_log_dir/.agent/current_user" 2>/dev/null || true
            _cpb_log_user="${_cpb_log_user%$'\r'}"
            [[ -n "$_cpb_log_extra" ]] && break
            case "$_cpb_log_user" in
                ""|"."|"..") break ;;
                [a-zA-Z0-9]*) ;;
                *) break ;;
            esac
            case "$_cpb_log_user" in
                *[!a-zA-Z0-9_.-]*) break ;;
            esac
            _cpb_log_lane="$_cpb_log_dir/.agent/$_cpb_log_user"
        fi
        [[ -d "$_cpb_log_lane" ]] || break

        print -r -- "$(date '+%Y-%m-%d %H:%M:%S') | AGENT | $_cpb_log_cmd" >> "$_cpb_log_lane/bash_history"
        break
    fi
    _cpb_log_dir="${_cpb_log_dir:h}"
done
unset _cpb_log_dir _cpb_log_cmd _cpb_log_lane _cpb_log_user _cpb_log_extra
