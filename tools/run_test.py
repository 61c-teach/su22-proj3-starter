from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path
import argparse
import csv
import hashlib
import os
import re
import subprocess
import sys
import textwrap
import time
import traceback

from diff_output import diff_output
from fetch_encoding import update_imm_circ


CIRC_IMPORT_REGEX = re.compile(r"desc=\"file#([^\"]+?\.circ)\"")

proj_dir_path = Path(__file__).parent.parent
tests_dir_path = proj_dir_path / "tests"
logisim_path = proj_dir_path / "tools" / "logisim-evolution.jar"

banned_component_names = [
    "Pull Resistor",
    "Transistor",
    "Transmission Gate",
    "Power",
    "POR",
    "Ground",
    "Divider",
    "Random",
    "PLA",
    "RAM",
    "Random Generator",
]
known_imports_dict = {
    "cpu/cpu.circ": [
        "cpu/alu.circ",
        "cpu/branch-comp.circ",
        "cpu/control-logic.circ",
        "cpu/imm-gen.circ",
        "cpu/regfile.circ",
    ],
    "harnesses/alu-harness.circ": [
        "cpu/alu.circ",
    ],
    "harnesses/imm-gen-harness.circ": [
        "cpu/imm-gen.circ",
    ],
    "harnesses/branch-comp-harness.circ": [
        "cpu/branch-comp.circ",
    ],
    "harnesses/cpu-harness.circ": [
        "cpu/cpu.circ",
        "cpu/mem.circ",
    ],
    "harnesses/regfile-harness.circ": [
        "cpu/regfile.circ",
    ],
    "harnesses/run.circ": [
        "harnesses/cpu-harness.circ",
    ],
    "tests/unit-alu/*.circ": [
        "cpu/alu.circ",
    ],
    "tests/unit-regfile/*.circ": [
        "cpu/regfile.circ",
    ],
    "tests/unit-partial-load/*.circ": [
        "cpu/partial-load.circ",
    ],
    "tests/integration-*/*.circ": [
        "harnesses/cpu-harness.circ",
    ],
}


def find_banned(circ_path):
    if circ_path.name in ["mem.circ"]:
        return False
    with circ_path.open("r") as f:
        contents = f.read()
    found = False
    for component_name in banned_component_names:
        if re.search(rf'<comp.*\bname="{component_name}"', contents):
            print(
                f"ERROR: found banned element ({component_name}) in {circ_path.as_posix()}"
            )
            found = True
    return found


starter_file_hashes = {
    "harnesses/cpu-harness.circ": "bb6df75d84088001121cb64ad39fdb8a",
    "harnesses/run.circ": "b452b26bc63a6e62b3ecead272682817",
    "tests/integration-addi/addi-basic.circ": "7036b58703bd9df01642a9d6210866f8",
    "tests/integration-addi/addi-negative.circ": "8fb256e6f8640f060d301ea4c4567085",
    "tests/integration-addi/addi-positive.circ": "036e6988bb5d2074b85cdc2faff0a7f4",
    "tests/integration-addi/out/addi-basic.piperef": "5adef575722b93ed04c82a37618ef2eb",
    "tests/integration-addi/out/addi-basic.ref": "4e1cfb0543418d95d3c75b49224c394d",
    "tests/integration-addi/out/addi-negative.piperef": "880daf267a095c2ab6012186be29aea6",
    "tests/integration-addi/out/addi-negative.ref": "8c4f4354e017d347a246b549c0a7c019",
    "tests/integration-addi/out/addi-positive.piperef": "554b05ad4952e65e3ac8844f36c0394e",
    "tests/integration-addi/out/addi-positive.ref": "079f17180d45eb7104220427f6360412",
    "tests/unit-alu/alu-add.circ": "a1853302b1853af8594042e9d22cfdfc",
    "tests/unit-alu/alu-all.circ": "0d319e6da7fc7a2c26d38e6d70e3848d",
    "tests/unit-alu/alu-logic.circ": "88fe241350eb7a7f93ff960feae4115b",
    "tests/unit-alu/alu-mult.circ": "5776b0a49a90b45c100714e2207e175e",
    "tests/unit-alu/alu-shift.circ": "4b735dc00f95e324f51777a3a72e736a",
    "tests/unit-alu/alu-slt-sub-bsel.circ": "7f8124ce02fcf6726df81a09c9181ad7",
    "tests/unit-alu/out/alu-add.ref": "cd03531fa04dbdefbc82bb9c4d3c9ed4",
    "tests/unit-alu/out/alu-all.ref": "97b8eaac983e2774b9b47bff46d41748",
    "tests/unit-alu/out/alu-logic.ref": "e655cd4b64a1d6bf6de02882d3708203",
    "tests/unit-alu/out/alu-mult.ref": "ef42ed4cf4c85efc3f1e7a414e2012b1",
    "tests/unit-alu/out/alu-shift.ref": "09a61bb272d6563dcfa35d76840765ec",
    "tests/unit-alu/out/alu-slt-sub-bsel.ref": "a27b828a9e4d093aaff92b7183cd9fb0",
    "tests/unit-regfile/out/regfile-more-regs.ref": "130731ddabf0a2c0d93e086cb57acc72",
    "tests/unit-regfile/out/regfile-read-only.ref": "64b6676bbb583d6a20564061cf680501",
    "tests/unit-regfile/out/regfile-read-write.ref": "127daa136c232f0ea623323d85b45972",
    "tests/unit-regfile/out/regfile-x0.ref": "e3025d4271c70ed8a32518fb95de63a8",
    "tests/unit-regfile/regfile-more-regs.circ": "d0397ca2a7e0bd63ddac8cdc926b45ad",
    "tests/unit-regfile/regfile-read-only.circ": "db07813ddbc5dab5253bb644b4e23467",
    "tests/unit-regfile/regfile-read-write.circ": "621b2d5881fee69f6650f75d04b6e31f",
    "tests/unit-regfile/regfile-x0.circ": "025919e544ad2f446eaddd25deecb100",
}


class TestCase:
    def __init__(self, circ_path, name=None):
        self.circ_path = Path(circ_path)
        self.id = str(self.circ_path)
        self.name = name or self.circ_path.stem

    def can_pipeline(self):
        if self.circ_path.match("unit-*/*.circ"):
            return False
        return True

    def fix_circ(self):
        fix_circ(self.circ_path)

    def check_hashes(self, pipelined=False):
        passed, reason = check_hash(self.circ_path)
        if not passed:
            return passed, reason

        passed, reason = check_hash(self.get_expected_table_path(pipelined=pipelined))
        if not passed:
            return passed, reason

        passed, reason = check_hash(proj_dir_path / "harnesses" / "cpu-harness.circ")
        if not passed:
            return passed, reason

        passed, reason = check_hash(proj_dir_path / "harnesses" / "run.circ")
        if not passed:
            return passed, reason

        return (True, "Circuit data matches starter code")

    def get_actual_table_path(self):
        return self.circ_path.parent / "out" / f"{self.name}.out"

    def get_expected_table_path(self, pipelined=False):
        path = self.circ_path.parent / "out" / f"{self.name}.ref"
        if pipelined:
            path = path.with_name(f"{self.name}.piperef")
        return path

    def run(self, pipelined=False):
        self.fix_circ()
        passed, reason = self.check_hashes(pipelined=pipelined)
        if not passed:
            return passed, reason, None

        if pipelined and not self.can_pipeline():
            pipelined = False
        passed = False
        proc = None

        output_path = self.get_actual_table_path()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output_path.open("w") as output_file:
                proc = subprocess.Popen(
                    [
                        "java",
                        "-jar",
                        str(logisim_path),
                        "-tty",
                        "table,binary,csv",
                        str(self.circ_path),
                    ],
                    stdout=output_file,
                    encoding="utf-8",
                    errors="ignore",
                )
                proc.wait()

            with redirect_stdout(StringIO()) as s:
                passed = diff_output(self.circ_path, pipelined)
                s.flush()
                s.seek(0)
                diff = s.read().strip("\n")

            if not passed:
                return False, "Did not match expected output", diff
            return True, "Matched expected output", None
        except KeyboardInterrupt:
            kill_proc(proc)
            sys.exit(1)
        except:
            traceback.print_exc()
            kill_proc(proc)
        return False, "Errored while running test", None


def fix_circ(circ_path):
    circ_path = circ_path.resolve()

    for glob, known_imports in known_imports_dict.items():
        if circ_path.match(glob):
            old_data = None
            data = None
            is_modified = False
            with circ_path.open("r", encoding="utf-8") as test_circ:
                old_data = test_circ.read()
                data = old_data
            for match in re.finditer(CIRC_IMPORT_REGEX, old_data):
                import_path_str = match.group(1)
                import_path = (circ_path.parent / Path(import_path_str)).resolve()
                for known_import in known_imports:
                    if import_path.match(known_import):
                        known_import_path = (proj_dir_path / known_import).resolve()
                        expected_import_path = Path(
                            os.path.relpath(known_import_path, circ_path.parent)
                        )
                        if import_path_str != expected_import_path.as_posix():
                            print(
                                f"Fixing bad import {import_path_str} in {circ_path.as_posix()} (should be {expected_import_path.as_posix()})"
                            )
                            data = data.replace(
                                import_path_str, expected_import_path.as_posix()
                            )
                            is_modified = True
                        break
                else:
                    expected_import_path = Path(
                        os.path.relpath(import_path, circ_path.parent)
                    )
                    if import_path_str != expected_import_path.as_posix():
                        print(
                            f"Fixing probably bad import {import_path_str} in {circ_path.as_posix()} (should be {expected_import_path.as_posix()})"
                        )
                        data = data.replace(
                            import_path_str, expected_import_path.as_posix()
                        )
                        is_modified = True
            if is_modified:
                with circ_path.open("w", encoding="utf-8") as test_circ:
                    test_circ.write(data)
            break


def run_tests(search_paths, pipelined=False):
    circ_paths = []
    for search_path in search_paths:
        if search_path.is_file() and search_path.suffix == ".circ":
            circ_paths.append(search_path)
            continue
        for circ_path in search_path.rglob("*.circ"):
            circ_paths.append(circ_path)
    circ_paths = sorted(circ_paths)

    has_banned_circuit = False
    for circ_path in proj_dir_path.rglob("cpu/*.circ"):
        fix_circ(circ_path)
        if find_banned(circ_path):
            has_banned_circuit = True
    for circ_path in proj_dir_path.rglob("harnesses/*.circ"):
        fix_circ(circ_path)
    if has_banned_circuit:
        return

    failed_tests = []
    passed_tests = []
    for circ_path in circ_paths:
        if "imm" in str(circ_path):
            update_imm_circ()

        test = TestCase(circ_path)
        did_pass, reason = False, "Unknown test error"
        try:
            did_pass, reason, extra = test.run(pipelined=pipelined)
        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise
        except:
            traceback.print_exc()
        if did_pass:
            print(f"PASS: {test.id}", flush=True)
            passed_tests.append(test)
        else:
            print(f"FAIL: {test.id} ({reason})", flush=True)
            failed_tests.append(test)
        if extra:
            print(textwrap.indent(extra, "  "), flush=True)

    print(
        f"Passed {len(passed_tests)}/{len(failed_tests) + len(passed_tests)} tests",
        flush=True,
    )


def check_hash(path):
    rel_path = path.resolve().relative_to(proj_dir_path.resolve())
    rel_path_str = rel_path.as_posix()
    if rel_path_str not in starter_file_hashes:
        return (True, f"Starter does not have hash for {rel_path_str}")
    with path.open("rb") as f:
        contents = f.read()
    contents = contents.replace(b"\r\n", b"\n")
    hashed_val = hashlib.md5(contents).hexdigest()
    if hashed_val != starter_file_hashes[rel_path_str]:
        return (False, f"{rel_path_str} was changed from starter")
    return (True, f"{rel_path_str} matches starter file")


def kill_proc(proc):
    if proc.poll() is None:
        proc.terminate()
        for _ in range(10):
            if proc.poll() is not None:
                return
            time.sleep(0.1)
    if proc.poll() is None:
        proc.kill()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Logisim tests")
    parser.add_argument(
        "test_path",
        help="Path to a test circuit, or a directory containing test circuits",
        type=Path,
        nargs="+",
    )
    parser.add_argument(
        "-p",
        "--pipelined",
        help="Check against reference output for 2-stage pipeline (when applicable)",
        action="store_true",
        default=False,
    )
    args = parser.parse_args()

    run_tests(args.test_path, args.pipelined)
