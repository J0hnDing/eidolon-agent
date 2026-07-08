# Weather Outfit Planner

Weather Outfit Planner is an automation tool skill for the Tools page. It accepts a city and unit system, fetches current weather from wttr.in, and returns concise commute-oriented outfit guidance.

## Inputs

- `city`: Required city name supported by wttr.in. The value must be a non-empty string and no more than 120 characters.
- `units`: Required temperature unit system. Supported values are `metric` and `imperial`.

## Outputs

The tool writes a JSON object with these fields:

- `recommendation`: Concise outfit recommendation for the current weather.
- `umbrella`: Boolean flag for whether an umbrella is recommended.
- `jacket`: Boolean flag for whether a jacket is recommended.
- `temperature`: Current temperature in the requested unit system.
- `condition`: Short current weather condition.
- `commute_note`: Brief commute-oriented note for the weather.

## Runtime Dependencies

Runtime weather data comes from `wttr.in` using the Python `requests` package. Running the skill requires approved network access to the explicit domain `wttr.in`.

The skill does not require secrets, shell access, browser automation, filesystem access, public posting, purchases, trading, file deletion, or automatic scheduling.

## Validation

Validation tests should mock wttr.in responses and should not require live internet access. Tests should verify input handling, output fields, permission declarations, and deterministic recommendation behavior from mocked weather data.

This proposed skill is not installed, enabled, approved to run, or scheduled automatically by the files in this package.
