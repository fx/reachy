# reachy-contracts

Shared wire types and golden fixtures for the Reachy Mini stack.

Every component that speaks the robot-link protocol depends on this package by
path, so a wire type has exactly one definition and the golden fixtures that pin
it are the same on both sides of the connection.

The wire types themselves arrive in
[change 0003](../../docs/changes/0003-contracts-package.md). What is here today
is the repository-wide version and the value type that parses it.
