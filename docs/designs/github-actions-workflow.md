# ---------------------------------------------------------------------------
# GitHub Actions basic usage example (documentation only, not parsed by HAL)
# ---------------------------------------------------------------------------
# To run HAL inside GitHub Actions, you define a workflow under .github/workflows.
# GitHub starts a "runner" (a temporary VM), checks out your repo, installs HAL,
# and then runs `hal run` commands against the checked-out workspace.
#
# Example: .github/workflows/hal-basic.yml
#
# name: HAL basic CI
#
# on:
#   push:
#     branches: [ main ]
#   pull_request:
#
# jobs:
#   hal-review:
#     runs-on: ubuntu-latest
#
#     steps:
#       - name: Checkout repository
#         uses: actions/checkout@v4
#
#       - name: Set up Python
#         uses: actions/setup-python@v5
#         with:
#           python-version: "3.11"
#
#       - name: Install HAL from this repo
#         run: |
#           pip install .
#
#       - name: Configure provider credentials
#         env:
#           ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
#         run: |
#           # HAL will read ANTHROPIC_API_KEY from the environment on the runner.
#           test -n "$ANTHROPIC_API_KEY"
#
#       - name: Run HAL headless review
#         run: |
#           hal run --json "Review this repository and report any issues in a concise summary" \
#             > hal_result.json
#
#       - name: Show HAL result
#         run: cat hal_result.json
#
# Notes:
# - This example assumes Anthropic as the provider; adjust `provider` and `model` above
#   and the credential environment variable (e.g., OPENAI_API_KEY) to match your setup.
# - In GitHub Actions, HAL behaves like any other CLI tool: it runs on the runner
#   (the temporary machine GitHub creates for your workflow) and operates on the
#   checked-out repository files.
