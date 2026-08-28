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

Updates copy the active version into a new draft/proposed version folder. Builder modifies only the copied draft and preserves its `function`, `service`, or `web_app` execution protocol. Tester validates the draft. Active function behavior, service schedule state, and a running active-version web application remain unchanged during update or repair. Service activation requires the stored schedule input to validate against the candidate schema and never overwrites edited schedule state. Web-app activation stops any old-version instance, then switches `active_version_id`, `installed_path`, and `manifest_path` after requirements pass. The next open lazily starts the newly active version.

## Maximum Versions

The local MVP allows at most three non-discarded versions per skill. Archived versions still count toward this cap because they remain switchable. The app must block new draft creation when the cap is reached and ask the user to delete an inactive version first. It must not silently delete versions.

## Permission Reapproval

If permissions, dependencies, and declared function requirements are unchanged, runtime reapproval can be skipped. A change to any of them creates a runtime review request for the candidate version before activation. Caller-target function approvals remain reusable only when each target's risk, permission/dependency contract, and JSON schemas retain the same backend fingerprint.

GitHub integration authorization is evaluated independently. A candidate version may reuse it only when provider, selected operation ids, normalized repository scope, and registry contract identity produce the same fingerprint. Any expansion blocks activation pending a new `integration_access` decision.

## User Controls

Skill Detail should show:

- active version;
- candidate/draft versions;
- changelogs;
- test and validation status;
- compare-with-active view across bounded text package files, including web application assets;
- activate button;
- discard/delete draft button.
