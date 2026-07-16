# Skill Versioning

Installed skills are versioned. The active version is never edited in place.

## Folder Layout

```text
skills/installed/<skill_name>/versions/v1/
skills/installed/<skill_name>/versions/v2/
skills/installed/<skill_name>/versions/v3/
```

## Version States

```text
active, draft, proposed_update, archived, discarded
```

## Update Rule

Updates copy the active version into a new draft/proposed version folder. Builder modifies only the copied draft and preserves its `function` or `web_app` execution protocol. Tester validates the draft. Active function behavior and a running active-version web application remain unchanged during update or repair. Activation stops any old-version web application instance, then switches `active_version_id`, `installed_path`, and `manifest_path` after requirements pass. The next open lazily starts the newly active version.

## Maximum Versions

The local MVP allows at most three non-discarded versions per skill. Archived versions still count toward this cap because they remain switchable. The app must block new draft creation when the cap is reached and ask the user to delete an inactive version first. It must not silently delete versions.

## Permission Reapproval

If permissions and dependencies are unchanged, runtime reapproval can be skipped. If permissions or dependencies change, the app creates a runtime approval request for the candidate version before activation.

## User Controls

Skill Detail should show:

- active version;
- candidate/draft versions;
- changelogs;
- test and validation status;
- compare-with-active view across bounded text package files, including web application assets;
- activate button;
- discard/delete draft button.
