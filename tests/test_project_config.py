from __future__ import annotations

import unittest

from devauto.core.models import ProjectConfig


class ProjectConfigTest(unittest.TestCase):
    def test_project_id_must_be_url_and_filename_safe(self) -> None:
        valid = ProjectConfig.from_mapping(
            {
                "id": "service_1.qa",
                "name": "Service",
                "repo": {"url": "/tmp/repo"},
                "docker": {"enabled": False},
            }
        )
        self.assertEqual(valid.id, "service_1.qa")

        for project_id in ["", "../bad", "bad/project", "bad project"]:
            with self.assertRaisesRegex(ValueError, "project id"):
                ProjectConfig.from_mapping(
                    {
                        "id": project_id,
                        "name": "Bad",
                        "repo": {"url": "/tmp/repo"},
                        "docker": {"enabled": False},
                    }
                )

    def test_policy_default_mode_must_be_supported(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {"enabled": False},
                "policy": {"default_mode": "auto"},
            }
        )
        self.assertEqual(project.policy.default_mode, "auto")

        with self.assertRaisesRegex(ValueError, "run mode"):
            ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": "/tmp/repo"},
                    "docker": {"enabled": False},
                    "policy": {"default_mode": "surprise"},
                }
            )

    def test_publish_mode_must_be_supported(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {"enabled": False},
                "publish": {"mode": "local_branch"},
            }
        )
        self.assertEqual(project.publish.mode, "local_branch")

        with self.assertRaisesRegex(ValueError, "publish.mode"):
            ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": "/tmp/repo"},
                    "docker": {"enabled": False},
                    "publish": {"mode": "surprise"},
                }
            )

    def test_boolean_settings_must_be_explicit_booleans(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {"enabled": "false"},
                "publish": {
                    "require_qa_approval": "false",
                    "require_human_approval": "true",
                },
            }
        )
        self.assertFalse(project.docker.enabled)
        self.assertFalse(project.publish.require_qa_approval)
        self.assertTrue(project.publish.require_human_approval)

        bad_configs = [
            {"docker": {"enabled": "maybe"}},
            {"publish": {"require_qa_approval": "maybe"}},
            {"publish": {"require_human_approval": "maybe"}},
        ]
        for config in bad_configs:
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, "boolean"):
                    ProjectConfig.from_mapping(
                        {
                            "id": "fixture",
                            "name": "Fixture",
                            "repo": {"url": "/tmp/repo"},
                            **config,
                        }
                    )

    def test_numeric_project_settings_must_be_safe(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {"enabled": False},
                "policy": {"max_inner_gate_fixes": "3", "max_outer_ai_fixes": "4"},
                "ai": {"executor": {"command": ["codex"], "timeout_sec": "30"}},
                "publish": {"deploy_timeout_sec": "60"},
                "workspace": {"keep_success_runs": "0", "keep_failed_runs": "5"},
            }
        )
        self.assertEqual(project.policy.max_inner_gate_fixes, 3)
        self.assertEqual(project.policy.max_outer_ai_fixes, 4)
        self.assertEqual(project.ai["executor"].timeout_sec, 30)
        self.assertEqual(project.publish.deploy_timeout_sec, 60)
        self.assertEqual(project.workspace.keep_success_runs, 0)
        self.assertEqual(project.workspace.keep_failed_runs, 5)

        bad_configs = [
            ({"policy": {"max_inner_gate_fixes": -1}}, "policy.max_inner_gate_fixes"),
            ({"policy": {"max_outer_ai_fixes": "bad"}}, "policy.max_outer_ai_fixes"),
            ({"ai": {"executor": {"command": ["codex"], "timeout_sec": 0}}}, "ai.executor.timeout_sec"),
            ({"ai": {"executor": {"command": ["codex"], "timeout_sec": "bad"}}}, "ai.executor.timeout_sec"),
            ({"publish": {"deploy_timeout_sec": 0}}, "publish.deploy_timeout_sec"),
            ({"workspace": {"keep_success_runs": -1}}, "workspace.keep_success_runs"),
            ({"workspace": {"keep_failed_runs": "bad"}}, "workspace.keep_failed_runs"),
        ]
        for config, pattern in bad_configs:
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, pattern):
                    ProjectConfig.from_mapping(
                        {
                            "id": "fixture",
                            "name": "Fixture",
                            "repo": {"url": "/tmp/repo"},
                            "docker": {"enabled": False},
                            **config,
                        }
                    )

    def test_project_contract_sections_must_have_expected_shapes(self) -> None:
        bad_configs = [
            ({"repo": "not a mapping"}, "repo"),
            ({"policy": []}, "policy"),
            ({"policy": {"forbidden_paths": ".env"}}, "policy.forbidden_paths"),
            ({"policy": {"high_risk_paths": "auth/**"}}, "policy.high_risk_paths"),
            ({"policy": {"forbidden_commands": "sudo"}}, "policy.forbidden_commands"),
            ({"ai": []}, "ai"),
            ({"ai": {"executor": "codex"}}, "ai.executor"),
            ({"ai": {"executor": {"command": "codex"}}}, "ai.executor.command"),
            ({"commands": []}, "commands"),
            ({"commands": {"unit_test": ["pytest"]}}, "commands.unit_test"),
            ({"docker": []}, "docker"),
            ({"docker": {"compose_files": "docker-compose.yml"}}, "docker.compose_files"),
            ({"publish": []}, "publish"),
            ({"workspace": []}, "workspace"),
        ]
        for config, pattern in bad_configs:
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, pattern):
                    ProjectConfig.from_mapping(
                        {
                            "id": "fixture",
                            "name": "Fixture",
                            "repo": {"url": "/tmp/repo"},
                            "docker": {"enabled": False},
                            **config,
                        }
                    )

    def test_repo_url_must_not_include_http_credentials(self) -> None:
        for repo_url in [
            "https://user:token@example.test/org/repo.git",
            "https://token@example.test/org/repo.git",
            "ssh://git:secret@example.test/org/repo.git",
        ]:
            with self.subTest(repo_url=repo_url):
                with self.assertRaisesRegex(ValueError, "repo.url"):
                    ProjectConfig.from_mapping(
                        {
                            "id": "fixture",
                            "name": "Fixture",
                            "repo": {"url": repo_url},
                            "docker": {"enabled": False},
                        }
                    )

        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "git@example.test:org/repo.git"},
                "docker": {"enabled": False},
            }
        )
        self.assertEqual(project.repo_url, "git@example.test:org/repo.git")

    def test_docker_compose_files_must_stay_inside_workspace(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {
                    "enabled": True,
                    "compose_files": ["docker-compose.yml", "docker/compose.override.yml"],
                    "env_file": "config/qa.env",
                },
            }
        )
        self.assertEqual(project.docker.compose_files, ["docker-compose.yml", "docker/compose.override.yml"])
        self.assertEqual(project.docker.env_file, "config/qa.env")

        for value in ["/tmp/docker-compose.yml", "../docker-compose.yml", "docker/../compose.yml", "~/compose.yml", ""]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "docker.compose_files"):
                    ProjectConfig.from_mapping(
                        {
                            "id": "fixture",
                            "name": "Fixture",
                            "repo": {"url": "/tmp/repo"},
                            "docker": {"enabled": True, "compose_files": [value]},
                        }
                    )

    def test_relative_docker_env_file_must_stay_inside_workspace(self) -> None:
        for value in ["/tmp/qa.env", "~/.devauto/secrets/qa.env"]:
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": "/tmp/repo"},
                    "docker": {"enabled": True, "env_file": value},
                }
            )
            self.assertEqual(project.docker.env_file, value)

        for value in ["../qa.env", "config/../qa.env"]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "docker.env_file"):
                    ProjectConfig.from_mapping(
                        {
                            "id": "fixture",
                            "name": "Fixture",
                            "repo": {"url": "/tmp/repo"},
                            "docker": {"enabled": True, "env_file": value},
                        }
                    )

    def test_docker_runtime_identifiers_and_ports_must_be_safe(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {
                    "enabled": True,
                    "project_name_prefix": "devauto_qa-1",
                    "preview_service": "app.web",
                    "preview_container_port": 8080,
                    "host_bind_ip": "0.0.0.0",
                },
            }
        )
        self.assertEqual(project.docker.project_name_prefix, "devauto_qa-1")
        self.assertEqual(project.docker.preview_service, "app.web")
        self.assertEqual(project.docker.preview_container_port, 8080)
        self.assertEqual(project.docker.host_bind_ip, "0.0.0.0")

        bad_configs = [
            {"docker": {"project_name_prefix": "-bad"}},
            {"docker": {"project_name_prefix": "Bad"}},
            {"docker": {"project_name_prefix": "bad prefix"}},
            {"docker": {"preview_service": "-bad"}},
            {"docker": {"preview_service": "bad service"}},
            {"docker": {"preview_container_port": 0}},
            {"docker": {"preview_container_port": 70000}},
            {"docker": {"preview_container_port": True}},
            {"docker": {"preview_container_port": 3000.5}},
            {"docker": {"host_bind_ip": "localhost"}},
            {"docker": {"host_bind_ip": "127.0.0.1:3000"}},
            {"docker": {"host_bind_ip": "::1"}},
        ]
        for config in bad_configs:
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, "docker\\."):
                    ProjectConfig.from_mapping(
                        {
                            "id": "fixture",
                            "name": "Fixture",
                            "repo": {"url": "/tmp/repo"},
                            **config,
                        }
                    )

    def test_git_branch_settings_must_be_safe(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo", "default_branch": "feature/ok", "branch_prefix": "aiqa/"},
                "docker": {"enabled": False},
            }
        )
        self.assertEqual(project.default_branch, "feature/ok")
        self.assertEqual(project.branch_prefix, "aiqa/")

        bad_configs = [
            {"repo": {"default_branch": "-bad"}},
            {"repo": {"default_branch": "bad branch"}},
            {"repo": {"default_branch": "bad..branch"}},
            {"repo": {"branch_prefix": "-bad/"}},
            {"repo": {"branch_prefix": "bad prefix/"}},
        ]
        for config in bad_configs:
            with self.assertRaisesRegex(ValueError, "branch"):
                ProjectConfig.from_mapping(
                    {
                        "id": "fixture",
                        "name": "Fixture",
                        "repo": {"url": "/tmp/repo", **config["repo"]},
                        "docker": {"enabled": False},
                    }
                )

    def test_command_and_ai_role_names_must_be_safe(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {"enabled": False},
                "commands": {"unit_test": "pytest"},
                "ai": {"reviewer_a": {"command": ["codex", "exec"]}},
            }
        )
        self.assertEqual(project.commands, {"unit_test": "pytest"})
        self.assertIn("reviewer_a", project.ai)

        bad_configs = [
            {"commands": {"../gate": "pytest"}},
            {"commands": {"bad gate": "pytest"}},
            {"commands": {"": "pytest"}},
            {"ai": {"reviewer/a": {"command": ["codex", "exec"]}}},
            {"ai": {"bad role": {"command": ["codex", "exec"]}}},
            {"ai": {"": {"command": ["codex", "exec"]}}},
        ]
        for config in bad_configs:
            with self.assertRaisesRegex(ValueError, "name"):
                ProjectConfig.from_mapping(
                    {
                        "id": "fixture",
                        "name": "Fixture",
                        "repo": {"url": "/tmp/repo"},
                        "docker": {"enabled": False},
                        **config,
                    }
                )

    def test_commands_must_not_contain_inline_secrets(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {"enabled": False},
                "commands": {"unit_test": "pytest -k token"},
                "ai": {"executor": {"command": ["codex", "exec", "--profile", "qa"]}},
                "publish": {
                    "mode": "deploy_command",
                    "require_human_approval": True,
                    "deploy_command": "./scripts/deploy-staging.sh",
                },
            }
        )
        self.assertEqual(project.commands["unit_test"], "pytest -k token")

        bad_configs = [
            {"commands": {"unit_test": "curl --token abc123"}},
            {"commands": {"unit_test": "OPENAI_API_KEY=abc123 pytest"}},
            {"commands": {"unit_test": "git clone https://token@example.test/org/repo.git"}},
            {"ai": {"executor": {"command": ["codex", "exec", "--api-key", "abc123"]}}},
            {
                "publish": {
                    "mode": "deploy_command",
                    "require_human_approval": True,
                    "deploy_command": "./deploy --password=abc123",
                }
            },
        ]
        for config in bad_configs:
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, "inline secret"):
                    ProjectConfig.from_mapping(
                        {
                            "id": "fixture",
                            "name": "Fixture",
                            "repo": {"url": "/tmp/repo"},
                            "docker": {"enabled": False},
                            **config,
                        }
                    )

    def test_commands_must_not_contain_forbidden_side_effects(self) -> None:
        bad_configs = [
            {"commands": {"unit_test": "git -C repo commit -m change"}},
            {"commands": {"unit_test": "git -C repo push origin main"}},
            {"ai": {"executor": {"command": ["git", "-C", "repo", "commit", "-m", "change"]}}},
            {
                "publish": {
                    "mode": "deploy_command",
                    "require_human_approval": True,
                    "deploy_command": "git -C repo commit -m release",
                }
            },
        ]
        for config in bad_configs:
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, "금지된 command"):
                    ProjectConfig.from_mapping(
                        {
                            "id": "fixture",
                            "name": "Fixture",
                            "repo": {"url": "/tmp/repo"},
                            "docker": {"enabled": False},
                            **config,
                        }
                    )


if __name__ == "__main__":
    unittest.main()
