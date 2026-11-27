# skill-writer checklist - using-pwsh (2026-11-27, exit 78 and the object model)

Change: the exit-code list gains exit 78 (a configuration `config` or `logdemo` cannot load, or a
permission setting `config-deploy` refuses, one `Error:` line per problem). The "Every public name"
paragraph gains the object model (`Pipeline`, `PSRecord`), the enums `AceType`, `AclEntryKind` and
`FileItemType` with one-phrase meanings, and the records `CimInstance`, `CimMethodResult`,
`PSObjectRecord`, `PSInvocationResult`. Both paragraphs reflowed to 100 columns, code spans kept
whole (word sequence asserted unchanged). Frontmatter unchanged; no other section touched. The
bitranox marketplace mirror (`coding-python-pwshpy`) still carries the old text and needs the same
change.

- [x] Receipt held (`skill_receipt.py start meta-skill-writer`).
- [x] Skill type: reference. Test: RED as retrieval probes on the pre-edit text (an EDIT; the
      installed copy is the old text), GREEN as quote-back retrieval probes on the new text.
- [x] RED (pre-edit excerpts, `opus` and `haiku`): both answered NONE or guessed for the exit code
      of a configuration that cannot load (guesses: 1, 22), NONE for what the listing calls return
      and the base of every record, NONE for the input of `new_item`, and guessed `AceType` for the
      kind of ACL entry.
- [x] Mechanism claims checked against the code and by running it: `exit_codes.CONFIG_ERROR`
      (78), `config_load.require_config`, `cli_logdemo`, the `config` command;
      `pwshpy --set lib_log_rich.console_level=NOPE config` and `LOG_CONSOLE_LEVEL=NOPE pwshpy
      logdemo` each printed one `Error: lib_log_rich: ...` line and exited 78. `--set` is a root
      option (after the subcommand it is a usage error, exit 2). Names checked against the
      package: `AclEntryKind` = Trustee/Owner/User/OwningGroup/Group/Mask/Other, `AceType` =
      Allow/Deny, `FileItemType` = file/directory; `Pipeline` (domain/pipeline.py) and `PSRecord`
      (records.py) are in `pwshpy.__all__`.
- [x] GREEN quote-back (`haiku`, then `opus` effort medium, 3 questions each, 6 cells): both quoted
      "78 a configuration `config` or `logdemo` cannot load ...", "`Pipeline` (the lazy stream the
      listing calls return)", "`PSRecord` (the frozen base of every record)", "`FileItemType` (the
      input of `new_item`)" and "`AclEntryKind` (whose ACL entry: owner, user, group, mask, ...)"
      verbatim, and told `AceType` (allow or deny) apart from `AclEntryKind`.
- [x] Gaps DECLINED:
      - where `--set` goes and what makes it "bad": the Further reading table routes "Layered
        configuration (`--set`, profiles, env, `config-deploy`)" to `CONFIG.md`, which documents
        it; the skill does not restate the configuration reference;
      - `Pipeline` methods (`.where()`) and which calls are listing calls: the "Core idea" section
        names the chain (`.where(pred).select(fn).take(n).first() / .to_list()`) and the "Library"
        section shows it on `ps.get_service()`; the probe saw only the names paragraph;
      - whether "From `pwshpy`:" covers the whole paragraph: it opens the paragraph and every name
        in it is in `pwshpy.__all__`;
      - "Errors: listed above": points at the errors section, outside the excerpt;
      - the exit 13 phrasing: unchanged by this edit, reviewed in the 2026-10-28 checklist.
- [x] Width check: no added line over 100 columns, no fence opened or closed by the reflow.
- [x] Version-history finder over the added lines: no hit (control phrase hits).
- [x] Address sweep over the added lines: no address, MAC, home or temp path.
- [x] No session narrative or private provenance in the skill or this artifact.

Decisions: none - every change follows from the code and the probe results above.
