# Fish completions for sshm — https://github.com/revsearch/sshm
#
# Install:  sshm completions fish > ~/.config/fish/completions/sshm.fish && exec fish

function __sshm_aliases --description 'Host aliases from ~/.ssh/config'
    set -l cfg "$HOME/.ssh/config"
    test -r "$cfg"
    and awk 'tolower($1) == "host" { for (i = 2; i <= NF; i++) if ($i !~ /[*?]/) print $i }' "$cfg"
end

function __sshm_wants_alias --description 'True when the previous token expects a host alias'
    set -l toks (commandline -opc)
    contains -- "$toks[-1]" connect c remove r rename mv enable e disable d
end

# port has a flexible order: `port <alias> a|r ...` or `port a|r <alias> ...`.
# These predicates scan the tokens after the `port` keyword so each piece is only
# offered while it's still missing.
function __sshm_port_post_tokens
    set -l toks (commandline -opc)
    set -l i (contains -i -- port $toks; or contains -i -- po $toks; or contains -i -- p $toks)
    test -n "$i"; or return
    set -l rest $toks[(math $i + 1)..-1]
    test (count $rest) -gt 0; and printf '%s\n' $rest
end

function __sshm_port_wants_alias --description 'port subcommand still needs its host alias'
    __fish_seen_subcommand_from port po p; or return 1
    for t in (__sshm_port_post_tokens)
        switch $t
            case add a remove r rm '-*'
            case '*'
                return 1  # a bare token already given — that's the alias
        end
    end
    return 0
end

function __sshm_port_wants_action --description 'port subcommand has no add/remove yet'
    __fish_seen_subcommand_from port po p; or return 1
    for t in (__sshm_port_post_tokens)
        contains -- $t add a remove r rm; and return 1
    end
    return 0
end

function __sshm_port_has_action --description 'port subcommand already has add/remove'
    __fish_seen_subcommand_from port po p; and not __sshm_port_wants_action
end

function __sshm_port_wants_flag --description 'port has an action but no -L/-R/-D yet'
    __sshm_port_has_action; or return 1
    for t in (__sshm_port_post_tokens)
        contains -- $t -L -R -D; and return 1
    end
    return 0
end

# Positional (non-flag) tokens after the first of the given subcommand keyword(s),
# used to offer an argument only while its slot is still empty.
function __sshm_post_tokens
    set -l toks (commandline -opc)
    set -l i
    for kw in $argv
        set i (contains -i -- $kw $toks)
        test -n "$i"; and break
    end
    test -n "$i"; or return
    for t in $toks[(math $i + 1)..-1]
        string match -q -- '-*' $t; or echo $t
    end
end

function __sshm_list_wants_arg
    __fish_seen_subcommand_from list l; or return 1
    set -l p (__sshm_post_tokens list l)
    test (count $p) -eq 0
end

function __sshm_export_wants_file
    __fish_seen_subcommand_from export; or return 1
    set -l p (__sshm_post_tokens export)
    test (count $p) -eq 0
end

function __sshm_export_wants_alias
    __fish_seen_subcommand_from export; or return 1
    set -l p (__sshm_post_tokens export)
    test (count $p) -ge 1
end

function __sshm_import_wants_file
    __fish_seen_subcommand_from import; or return 1
    set -l p (__sshm_post_tokens import)
    test (count $p) -eq 0
end

# Don't fall back to file completion unless a rule opts in (-F).
complete -c sshm -f
complete -c sshm -n __fish_use_subcommand -l help -d 'Show help'

# --- top level: subcommands + host aliases (bare `sshm <alias>` connects) ---
complete -c sshm -n __fish_use_subcommand -a '(__sshm_aliases)' -d 'Connect to host'
complete -c sshm -n __fish_use_subcommand -a list      -d 'List hosts or sessions'
complete -c sshm -n __fish_use_subcommand -a connect   -d 'Attach to a session'
complete -c sshm -n __fish_use_subcommand -a add       -d 'Add a server'
complete -c sshm -n __fish_use_subcommand -a remove    -d 'Remove a host'
complete -c sshm -n __fish_use_subcommand -a rename    -d 'Rename an alias'
complete -c sshm -n __fish_use_subcommand -a port      -d 'Port forward / SOCKS proxy'
complete -c sshm -n __fish_use_subcommand -a enable    -d 'Keep session alive'
complete -c sshm -n __fish_use_subcommand -a disable   -d 'Stop auto-connect'
complete -c sshm -n __fish_use_subcommand -a export    -d 'Export hosts to JSON'
complete -c sshm -n __fish_use_subcommand -a import    -d 'Import hosts from JSON'
complete -c sshm -n __fish_use_subcommand -a status    -d 'Daemon status'
complete -c sshm -n __fish_use_subcommand -a stop      -d 'Stop the daemon'
complete -c sshm -n __fish_use_subcommand -a install   -d 'Autostart on login'
complete -c sshm -n __fish_use_subcommand -a uninstall -d 'Remove autostart'

# --- host alias as the argument to alias-taking commands ---
complete -c sshm -n __sshm_wants_alias -a '(__sshm_aliases)' -d host

# --- port: host alias (either order), action while missing, flag after the action ---
complete -c sshm -n __sshm_port_wants_alias -a '(__sshm_aliases)' -d host
complete -c sshm -n __sshm_port_wants_action -a 'add remove' -d action
complete -c sshm -n __sshm_port_wants_flag -a '-L -R -D' -d 'forward direction'

# --- list: a single arg — host alias or a .json file ---
complete -c sshm -n __sshm_list_wants_arg -a '(__sshm_aliases)' -d host
complete -c sshm -n __sshm_list_wants_arg -F

# --- export: <file> then host names ---
complete -c sshm -n __sshm_export_wants_file -F
complete -c sshm -n __sshm_export_wants_alias -a '(__sshm_aliases)' -d host

# --- import: <file>, then -o ---
complete -c sshm -n __sshm_import_wants_file -F
complete -c sshm -n '__fish_seen_subcommand_from import' -s o -l override -d 'Override existing hosts'
