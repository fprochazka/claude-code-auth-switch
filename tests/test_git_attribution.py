import os

COMPANY_ATTRIBUTION = """\
[claude-code "attribution"]
    commit = "Generated with Claude Code\\n\\nCo-Authored-By: Claude <noreply@example.com>"
    pr = "Generated with Claude Code"
    sessionUrl = false
"""

COMPANY_INCLUDE = """\
[includeIf "gitdir:~/devel/projects/company/"]
    path = ~/devel/projects/company/.gitconfig
"""

EXPECTED_COMPANY = {
    "attribution": {
        "commit": "Generated with Claude Code\n\nCo-Authored-By: Claude <noreply@example.com>",
        "pr": "Generated with Claude Code",
        "sessionUrl": False,
    }
}


def test_inside_repo_git_evaluates_include_if(sandbox):
    sandbox.write(".gitconfig", COMPANY_INCLUDE)
    sandbox.write("devel/projects/company/.gitconfig", COMPANY_ATTRIBUTION)
    repo = sandbox.git_init("devel/projects/company/service")

    result = sandbox.run_helper(repo)

    assert result.returncode == 0
    assert result.settings == EXPECTED_COMPANY
    assert result.stderr == ""


def test_inside_repo_subdirectory(sandbox):
    sandbox.write(".gitconfig", COMPANY_INCLUDE)
    sandbox.write("devel/projects/company/.gitconfig", COMPANY_ATTRIBUTION)
    repo = sandbox.git_init("devel/projects/company/service")
    subdir = repo / "src" / "module"
    subdir.mkdir(parents=True)

    assert sandbox.run_helper(subdir).settings == EXPECTED_COMPANY


def test_inside_repo_glob_pattern_is_evaluated_by_git(sandbox):
    sandbox.write(".gitconfig", '[includeIf "gitdir:**/company/**"]\n    path = ~/company.gitconfig\n')
    sandbox.write("company.gitconfig", '[claude-code "attribution"]\n    pr = "from glob"\n')
    repo = sandbox.git_init("devel/projects/company/service")

    assert sandbox.run_helper(repo).settings == {"attribution": {"pr": "from glob"}}


def test_outside_repo_parent_directory_prefix_match(sandbox):
    sandbox.write(".gitconfig", COMPANY_INCLUDE)
    sandbox.write("devel/projects/company/.gitconfig", COMPANY_ATTRIBUTION)
    company = sandbox.mkdir("devel/projects/company")
    nested = sandbox.mkdir("devel/projects/company/not-a-repo")

    assert sandbox.run_helper(company).settings == EXPECTED_COMPANY
    assert sandbox.run_helper(nested).settings == EXPECTED_COMPANY


def test_outside_repo_sibling_with_shared_prefix_does_not_match(sandbox):
    sandbox.write(".gitconfig", COMPANY_INCLUDE)
    sandbox.write("devel/projects/company/.gitconfig", COMPANY_ATTRIBUTION)
    sibling = sandbox.mkdir("devel/projects/company-other")

    result = sandbox.run_helper(sibling)

    assert result.returncode == 0
    assert result.stdout == ""


def test_no_match_prints_nothing(sandbox):
    sandbox.write(".gitconfig", COMPANY_INCLUDE)
    sandbox.write("devel/projects/company/.gitconfig", COMPANY_ATTRIBUTION)
    personal_repo = sandbox.git_init("devel/personal/project")
    personal_dir = sandbox.mkdir("devel/personal/notes")

    for directory in (personal_repo, personal_dir):
        result = sandbox.run_helper(directory)
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""


def test_global_config_values_are_the_base_and_includes_override_them(sandbox):
    sandbox.write(
        ".gitconfig",
        '[claude-code "attribution"]\n    commit = "global"\n    pr = "global"\n' + COMPANY_INCLUDE,
    )
    sandbox.write("devel/projects/company/.gitconfig", '[claude-code "attribution"]\n    pr = "company"\n')
    company = sandbox.mkdir("devel/projects/company/notes")
    elsewhere = sandbox.mkdir("elsewhere")

    assert sandbox.run_helper(company).settings == {"attribution": {"commit": "global", "pr": "company"}}
    assert sandbox.run_helper(elsewhere).settings == {"attribution": {"commit": "global", "pr": "global"}}


def test_value_after_the_include_wins_like_in_git(sandbox):
    sandbox.write(".gitconfig", COMPANY_INCLUDE + '[claude-code "attribution"]\n    pr = "after include"\n')
    sandbox.write("devel/projects/company/.gitconfig", '[claude-code "attribution"]\n    pr = "company"\n')
    company = sandbox.mkdir("devel/projects/company")
    repo = sandbox.git_init("devel/projects/company/service")

    assert sandbox.run_helper(company).settings == {"attribution": {"pr": "after include"}}
    assert sandbox.run_helper(repo).settings == {"attribution": {"pr": "after include"}}


def test_absolute_pattern_and_tilde_include_path(sandbox):
    company = sandbox.mkdir("work")
    sandbox.write(".gitconfig", f'[includeIf "gitdir:{company}/"]\n    path = ~/work.gitconfig\n')
    sandbox.write("work.gitconfig", '[claude-code "attribution"]\n    pr = "work"\n')

    assert sandbox.run_helper(company).settings == {"attribution": {"pr": "work"}}


def test_relative_include_path_and_dot_slash_pattern(sandbox):
    # The global config includes a file from ~/.config/git/, which declares the conditional include with paths relative to itself.
    sandbox.write(".gitconfig", "[include]\n    path = .config/git/main.gitconfig\n")
    sandbox.write(
        ".config/git/main.gitconfig",
        '[includeIf "gitdir:./projects/company/"]\n    path = ./company.gitconfig\n',
    )
    sandbox.write(".config/git/company.gitconfig", '[claude-code "attribution"]\n    pr = "relative"\n')
    company = sandbox.mkdir(".config/git/projects/company/notes")

    assert sandbox.run_helper(company).settings == {"attribution": {"pr": "relative"}}


def test_relative_include_path_without_dot_slash(sandbox):
    sandbox.write(".gitconfig", '[includeIf "gitdir:~/devel/projects/company/"]\n    path = devel/projects/company/.gitconfig\n')
    sandbox.write("devel/projects/company/.gitconfig", '[claude-code "attribution"]\n    pr = "relative"\n')
    company = sandbox.mkdir("devel/projects/company")

    assert sandbox.run_helper(company).settings == {"attribution": {"pr": "relative"}}


def test_nested_include_if_inside_matched_file(sandbox):
    sandbox.write(".gitconfig", COMPANY_INCLUDE)
    sandbox.write(
        "devel/projects/company/.gitconfig",
        '[claude-code "attribution"]\n    pr = "company"\n'
        '[includeIf "gitdir:~/devel/projects/company/team/"]\n    path = ./team.gitconfig\n',
    )
    sandbox.write("devel/projects/company/team.gitconfig", '[claude-code "attribution"]\n    pr = "team"\n')
    team = sandbox.mkdir("devel/projects/company/team")
    other = sandbox.mkdir("devel/projects/company/other")

    assert sandbox.run_helper(team).settings == {"attribution": {"pr": "team"}}
    assert sandbox.run_helper(other).settings == {"attribution": {"pr": "company"}}


def test_gitdir_i_matches_case_insensitively(sandbox):
    sandbox.write(".gitconfig", '[includeIf "gitdir/i:~/Devel/Projects/COMPANY/"]\n    path = ~/company.gitconfig\n')
    sandbox.write("company.gitconfig", '[claude-code "attribution"]\n    pr = "case"\n')
    company = sandbox.mkdir("devel/projects/company")

    assert sandbox.run_helper(company).settings == {"attribution": {"pr": "case"}}


def test_plain_gitdir_is_case_sensitive(sandbox):
    sandbox.write(".gitconfig", '[includeIf "gitdir:~/Devel/Projects/COMPANY/"]\n    path = ~/company.gitconfig\n')
    sandbox.write("company.gitconfig", '[claude-code "attribution"]\n    pr = "case"\n')
    company = sandbox.mkdir("devel/projects/company")

    assert sandbox.run_helper(company).stdout == ""


def test_outside_repo_glob_and_unanchored_patterns_are_skipped(sandbox):
    sandbox.write(
        ".gitconfig",
        '[includeIf "gitdir:~/devel/*/company/"]\n    path = ~/glob.gitconfig\n'
        '[includeIf "gitdir:company/"]\n    path = ~/unanchored.gitconfig\n'
        '[includeIf "gitdir:~/devel/projects/company"]\n    path = ~/no-slash.gitconfig\n',
    )
    for name in ("glob", "unanchored", "no-slash"):
        sandbox.write(f"{name}.gitconfig", f'[claude-code "attribution"]\n    pr = "{name}"\n')
    company = sandbox.mkdir("devel/projects/company")

    result = sandbox.run_helper(company)

    assert result.returncode == 0
    assert result.stdout == ""


def test_symlinked_directory_matches_via_realpath(sandbox):
    sandbox.write(".gitconfig", COMPANY_INCLUDE)
    sandbox.write("devel/projects/company/.gitconfig", COMPANY_ATTRIBUTION)
    target = sandbox.mkdir("devel/projects/company/notes")
    link = sandbox.home / "company-notes"
    link.symlink_to(target)

    assert sandbox.run_helper(link).settings == EXPECTED_COMPANY


def test_session_url_boolean_spellings(sandbox):
    company = sandbox.mkdir("devel/projects/company")
    sandbox.write(".gitconfig", COMPANY_INCLUDE)
    cases = {
        "true": True, "yes": True, "on": True, "1": True, "TRUE": True,
        "false": False, "no": False, "off": False, "0": False, '""': False,
    }
    for spelling, expected in cases.items():
        sandbox.write("devel/projects/company/.gitconfig", f'[claude-code "attribution"]\n    sessionUrl = {spelling}\n')
        assert sandbox.run_helper(company).settings == {"attribution": {"sessionUrl": expected}}, spelling

    # A key without "= value" is true in git.
    sandbox.write("devel/projects/company/.gitconfig", '[claude-code "attribution"]\n    sessionUrl\n')
    assert sandbox.run_helper(company).settings == {"attribution": {"sessionUrl": True}}


def test_invalid_session_url_is_dropped_with_a_warning(sandbox):
    sandbox.write(".gitconfig", COMPANY_INCLUDE)
    sandbox.write("devel/projects/company/.gitconfig", '[claude-code "attribution"]\n    sessionUrl = maybe\n    pr = "kept"\n')
    company = sandbox.mkdir("devel/projects/company")

    result = sandbox.run_helper(company)

    assert result.returncode == 0
    assert result.settings == {"attribution": {"pr": "kept"}}
    assert "sessionurl is not a boolean" in result.stderr


def test_unknown_key_is_ignored_with_a_warning(sandbox):
    sandbox.write(".gitconfig", '[claude-code "attribution"]\n    pr = "kept"\n    footer = "dropped"\n')
    directory = sandbox.mkdir("anywhere")

    result = sandbox.run_helper(directory)

    assert result.settings == {"attribution": {"pr": "kept"}}
    assert "unknown key claude-code.attribution.footer" in result.stderr


def test_empty_commit_is_kept_as_empty_string(sandbox):
    sandbox.write(".gitconfig", '[claude-code "attribution"]\n    commit = ""\n')
    directory = sandbox.mkdir("anywhere")

    assert sandbox.run_helper(directory).settings == {"attribution": {"commit": ""}}


def test_broken_git_config_does_not_fail(sandbox):
    sandbox.write(".gitconfig", "[claude-code \"attribution\"\n    this is not valid\n")
    directory = sandbox.mkdir("anywhere")

    result = sandbox.run_helper(directory)

    assert result.returncode == 0
    assert result.stdout == ""
    assert "cannot read attribution from git config" in result.stderr


def test_missing_directory_does_not_fail(sandbox):
    result = sandbox.run_helper(sandbox.home / "does-not-exist")

    assert result.returncode == 0
    assert result.stdout == ""


def test_output_is_valid_json_for_special_characters(sandbox):
    sandbox.write(".gitconfig", '[claude-code "attribution"]\n    pr = "quote \\" backslash \\\\ tab \\t emoji \U0001F916 }"\n')
    directory = sandbox.mkdir("anywhere")

    result = sandbox.run_helper(directory)

    assert result.settings == {"attribution": {"pr": 'quote " backslash \\ tab \t emoji \U0001F916 }'}}
    assert os.linesep not in result.stdout.rstrip("\n")
