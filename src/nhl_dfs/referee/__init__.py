"""Independent referee: its own CSV reading and its own roster rules.

Must not import nhl_dfs.intake, nhl_dfs.export, or nhl_dfs.contracts
(tests/test_referee.py enforces this), so a defect in the builder's code
cannot also hide in the check.
"""
