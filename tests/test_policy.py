from __future__ import annotations

import unittest

from devauto.core.models import PolicyConfig
from devauto.core.policy import (
    command_contains_inline_secret,
    command_is_allowed,
    command_matches_forbidden,
    output_command_is_allowed,
)


class PolicyCommandTest(unittest.TestCase):
    def test_forbidden_commands_match_shell_whitespace_variants(self) -> None:
        policy = PolicyConfig()

        self.assertFalse(command_is_allowed("git\tpush origin main", policy))
        self.assertFalse(command_is_allowed("git -C repo push origin main", policy))
        self.assertFalse(command_is_allowed("git -c user.name=qa push origin main", policy))
        self.assertFalse(command_is_allowed("git commit -m change", policy))
        self.assertFalse(command_is_allowed("git -C repo commit -m change", policy))
        self.assertFalse(command_is_allowed("docker   system\nprune", policy))
        self.assertFalse(command_is_allowed("ssh\tqa@example.test", policy))
        self.assertFalse(command_is_allowed("rm   -rf   /", policy))
        self.assertTrue(command_is_allowed("git status --short", policy))

    def test_forbidden_single_word_commands_use_command_boundaries(self) -> None:
        self.assertFalse(command_matches_forbidden("printf passh", "ssh "))
        self.assertTrue(command_matches_forbidden("ssh qa@example.test", "ssh "))
        self.assertTrue(command_matches_forbidden("ssh\tqa@example.test", "ssh "))

    def test_output_policy_keeps_deploy_mode_exception_but_blocks_push_obfuscation(self) -> None:
        policy = PolicyConfig()

        self.assertTrue(output_command_is_allowed("./scripts/deploy-staging.sh", policy))
        self.assertFalse(output_command_is_allowed("git   push origin main", policy))
        self.assertFalse(output_command_is_allowed("git -C repo push origin main", policy))
        self.assertFalse(output_command_is_allowed("git commit -m release", policy))
        self.assertFalse(output_command_is_allowed("./scripts/deploy-staging.sh --password abc123", policy))

    def test_inline_secret_detector_matches_secret_shapes(self) -> None:
        positives = [
            "curl --token abc123",
            "deploy --password=abc123",
            "OPENAI_API_KEY=abc123 codex exec",
            "curl -H 'Authorization: Bearer abc123' https://example.test",
            "git clone https://token@example.test/org/repo.git",
        ]
        for command in positives:
            with self.subTest(command=command):
                self.assertTrue(command_contains_inline_secret(command))

        negatives = [
            "pytest -k token",
            "echo Authorization docs",
            "deploy --tokenize output",
            "codex exec --profile qa",
        ]
        for command in negatives:
            with self.subTest(command=command):
                self.assertFalse(command_contains_inline_secret(command))

    def test_command_policy_blocks_inline_secret_shapes(self) -> None:
        policy = PolicyConfig()

        self.assertFalse(command_is_allowed("curl --token abc123", policy))
        self.assertFalse(command_is_allowed("OPENAI_API_KEY=abc123 codex exec", policy))


if __name__ == "__main__":
    unittest.main()
