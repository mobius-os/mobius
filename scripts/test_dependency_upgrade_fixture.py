"""Hermetic construction and fail-closed command contracts; no Docker needed."""

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "fixture", HERE / "dependency-upgrade-fixture.py"
)
assert spec and spec.loader
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class FixtureTests(unittest.TestCase):
    def test_pair_changes_real_image_base_and_platform_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend").mkdir()
            (root / "Dockerfile").write_text(
                "FROM scratch AS source\nFROM python:3.12-slim-trixie\n"
            )
            (root / "backend/requirements.txt").write_text("fastapi>=1\n")
            (root / "backend/requirements.lock").write_text("fastapi==1 \\\n    --hash=sha256:" + "a" * 64 + "\n")
            fixture.prepare(root, "old")
            self.assertIn(f"FROM {fixture.OLD_BASE}\n", (root / "Dockerfile").read_text())
            self.assertNotIn("colorama", (root / "backend/requirements.lock").read_text())
            fixture.prepare(root, "new", "b" * 64)
            self.assertIn(f"FROM {fixture.NEW_BASE}\n", (root / "Dockerfile").read_text())
            self.assertIn("colorama==0.4.6", (root / "backend/requirements.txt").read_text())
            self.assertIn(
                "colorama==0.4.6 \\\n    --hash=sha256:" + "b" * 64,
                (root / "backend/requirements.lock").read_text(),
            )

    def test_invalid_new_fixture_leaves_all_inputs_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend").mkdir()
            paths = ("Dockerfile", "backend/requirements.txt", "backend/requirements.lock")
            (root / paths[0]).write_text(f"FROM {fixture.OLD_BASE}\n")
            (root / paths[1]).write_text("fastapi>=1\n")
            (root / paths[2]).write_text("fastapi==1\n")
            before = {path: (root / path).read_bytes() for path in paths}
            with self.assertRaises(ValueError):
                fixture.prepare(root, "new", "not-a-hash")
            self.assertEqual(before, {path: (root / path).read_bytes() for path in paths})
            (root / paths[2]).write_text("colorama==0.4.6\n")
            before = {path: (root / path).read_bytes() for path in paths}
            with self.assertRaises(ValueError):
                fixture.prepare(root, "new", "b" * 64)
            self.assertEqual(before, {path: (root / path).read_bytes() for path in paths})

    def test_dependency_fixture_merges_with_preserved_real_input_notes(self):
        # Use the actual manifests, not a one-line stand-in: the harness promises
        # to preserve a local EOF customization through a zero-conflict update.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend").mkdir()
            for path in ("Dockerfile", "backend/requirements.txt", "backend/requirements.lock"):
                shutil.copyfile(HERE.parent / path, root / path)

            def git(*args):
                return subprocess.run(
                    ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@localhost",
                     "-c", "core.hooksPath=/dev/null", *args],
                    cwd=root, text=True, capture_output=True, check=True,
                ).stdout.strip()

            git("init", "-q")
            fixture.prepare(root, "old")
            git("add", ".")
            git("commit", "-qm", "old fixture")
            old = git("rev-parse", "HEAD")
            for path, note in (
                ("Dockerfile", "\n# upgrade-path: local image customization\n"),
                ("backend/requirements.txt", "# upgrade-path: local package note\n"),
            ):
                with (root / path).open("a") as stream:
                    stream.write(note)
            git("add", ".")
            git("commit", "-qm", "local notes")
            local = git("rev-parse", "HEAD")
            git("checkout", "-q", "--detach", old)
            fixture.prepare(root, "new", "b" * 64)
            git("add", ".")
            git("commit", "-qm", "new dependency fixture")
            tree = git("merge-tree", "--write-tree", local, "HEAD").splitlines()[0]
            self.assertIn("local image customization", git("show", f"{tree}:Dockerfile"))
            requirements = git("show", f"{tree}:backend/requirements.txt")
            self.assertIn("local package note", requirements)
            self.assertIn(fixture.PACKAGE, requirements)
            self.assertIn(fixture.PACKAGE, git("show", f"{tree}:backend/requirements.lock"))

    def test_failed_partial_image_build_is_cleaned_without_touching_other_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fakebin = root / "bin"
            fakebin.mkdir()
            log = root / "docker.log"
            docker = fakebin / "docker"
            docker.write_text("""#!/bin/bash
printf '%s\\n' "$*" >> "$DOCKER_LOG"
case "$*" in
  'info'|'buildx version') exit 0 ;;
  'image inspect '*) exit 1 ;;
  'buildx build '*) exit 23 ;;
  'image rm -f '*) exit 0 ;;
  *) echo "unexpected docker call" >&2; exit 99 ;;
esac
""")
            docker.chmod(0o755)
            source = root / "source"
            source.mkdir()
            (source / "Dockerfile").write_text("FROM python:3.12-slim-trixie\n")
            for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "public stand-in")):
                subprocess.run(
                    ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@localhost",
                     "-c", "core.hooksPath=/dev/null", *args], cwd=source, check=True,
                )
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
            proc = subprocess.run(
                ["bash", str(HERE / "test-dependency-upgrade-path.sh"), base, str(source)],
                env={**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
                     "DOCKER_LOG": str(log), "TMPDIR": str(root)},
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(proc.returncode, 23, proc.stderr)
            calls = log.read_text().splitlines()
            builds = [line for line in calls if line.startswith("buildx build ")]
            removed = [line.removeprefix("image rm -f ") for line in calls if line.startswith("image rm -f ")]
            self.assertEqual(len(builds), 1)
            expected = builds[0].split(" -t ", 1)[1].split()[0]
            self.assertEqual(removed, [expected])
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["bin", "docker.log", "source"])

    def test_index_requires_exact_published_wheel_hash(self):
        data = {"urls": [{"filename": fixture.WHEEL, "packagetype": "bdist_wheel",
                          "digests": {"sha256": "c" * 64}}]}
        self.assertEqual(fixture.wheel_hash(data), "c" * 64)
        with self.assertRaises(ValueError):
            fixture.wheel_hash({"urls": []})
        with self.assertRaises(ValueError):
            fixture.wheel_hash({"urls": data["urls"] * 2})

    def test_entrypoints_reject_incomplete_fixture_without_docker(self):
        proc = subprocess.run(
            ["bash", str(HERE / "test-upgrade-path.sh"), "old", "new"],
            env={"UPGRADE_PYTHON_BEFORE": "3.12.13"},
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("supply distinct Python", proc.stderr)
        proc = subprocess.run(
            ["bash", str(HERE / "test-dependency-upgrade-path.sh"), "not-a-sha"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("full base SHA", proc.stderr)

    def test_failed_partial_container_launch_removes_only_its_reserved_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            def git(*args):
                return subprocess.check_output(
                    ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@localhost",
                     "-c", "core.hooksPath=/dev/null", *args], cwd=source, text=True,
                ).strip()
            git("init", "-q")
            (source / "file").write_text("old")
            git("add", ".")
            git("commit", "-qm", "old")
            old = git("rev-parse", "HEAD")
            (source / "file").write_text("new")
            git("commit", "-qam", "new")
            new = git("rev-parse", "HEAD")
            fakebin = root / "bin"
            fakebin.mkdir()
            log = root / "docker.log"
            docker = fakebin / "docker"
            docker.write_text("""#!/bin/bash
printf '%s\\n' "$*" >> "$DOCKER_LOG"
case "$*" in
  'run --rm --entrypoint cat '*)
    [ "$5" = old ] && sha=$OLD_SHA || sha=$NEW_SHA
    printf '{"sha":"%s"}\\n' "$sha" ;;
  'run --rm --entrypoint git '*)
    [ "$5" = old ] && sha=$OLD_SHA || sha=$NEW_SHA
    printf '%s\\n' "$sha" ;;
  'image inspect -f '*)
    [ "${@: -1}" = old ] && sha=$OLD_SHA || sha=$NEW_SHA
    printf '%s\\n' "$sha" ;;
  'container inspect '*|'volume inspect '*) exit 1 ;;
  'volume create '*) exit 0 ;;
  'run -d '*) exit 23 ;;
  'rm -f '*|'volume rm '*) exit 0 ;;
  *) echo "unexpected docker call" >&2; exit 99 ;;
esac
""")
            docker.chmod(0o755)
            proc = subprocess.run(
                ["bash", str(HERE / "test-upgrade-path.sh"), "old", "new", str(source)],
                env={**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
                     "DOCKER_LOG": str(log), "TMPDIR": str(root), "OLD_SHA": old, "NEW_SHA": new},
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(proc.returncode, 23, proc.stderr)
            calls = log.read_text().splitlines()
            launch = next(line for line in calls if line.startswith("run -d "))
            name = launch.split(" --name ", 1)[1].split()[0]
            self.assertEqual([line for line in calls if line.startswith("rm -f ")], ["rm -f " + name])
            self.assertEqual([line for line in calls if line.startswith("volume rm ")], ["volume rm " + name + "-data"])
            self.assertEqual(git("for-each-ref", "refs/upgrade-path"), "")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["bin", "docker.log", "source"])

    def test_hosted_command_binds_each_image_to_its_local_source_commit(self):
        script = (HERE / "test-dependency-upgrade-path.sh").read_text()
        for required in (
            '--build-context "mobius-local-platform-source=$work/bundle"',
            '--build-arg "BUILD_SHA=$sha"',
            '--build-arg "MOBIUS_LOCAL_PLATFORM_SHA=$sha"',
            '--build-arg "MOBIUS_LOCAL_PLATFORM_BASE_SHA=$BASE"',
            'UPGRADE_FORCE_ROLLBACK=1',
            '"$root/scripts/test-upgrade-path.sh" "$old_image" "$new_image" "$source_dir"',
        ):
            self.assertIn(required, script)
        self.assertEqual(script.count('"$root/scripts/test-upgrade-path.sh"'), 2)

    def test_rollback_replays_target_image_activation_before_wrong_image_boot(self):
        script = (HERE / "test-upgrade-path.sh").read_text()
        rollback = script.split('if [ "$force_rollback" = 1 ]; then', 1)[1].split(
            'write_status replacing "Rebuilding the container."', 1
        )[0]
        # The outgoing image's cutover deliberately leaves an image-requiring
        # update prepared. The candidate image boots and swaps it; only then
        # may a boot of the previous (wrong) image exercise real reversion.
        ordered = (
            "cutover did not leave the image-requiring update prepared",
            'start "$CANDIDATE"',
            "target image boot did not swap the prepared source",
            'verify_fixture_service "$candidate"',
            'start "$PREVIOUS"',
            "the old image did not restore the pre-update source snapshot",
        )
        positions = [rollback.index(value) for value in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('[[ $(python_version) == "$after_python" ]]', rollback)
        self.assertIn('[[ $(python_version) == "$before_python" ]]', rollback)
        self.assertIn('grep -Fq "$lock_package" /data/platform/backend/requirements.lock', rollback)
        self.assertIn('rollback does not run the previous image', rollback)
        self.assertIn('the rolled-back update is not kept prepared for retry', rollback)


if __name__ == "__main__":
    unittest.main()
