"""--enable-repair: Standard AUS; ohne Flag existiert kein Supervisor."""
from kefplayer import cli


def test_flag_defaults_to_off():
    args = cli.build_parser().parse_args(["play", "x.flac"])
    assert args.enable_repair is False


def test_flag_can_be_enabled():
    args = cli.build_parser().parse_args(["play", "--enable-repair", "x.flac"])
    assert args.enable_repair is True


def test_enable_repair_conflicts_with_no_health():
    import pytest
    args = cli.build_parser().parse_args(["play", "--enable-repair", "--no-health", "x.flac"])
    with pytest.raises(SystemExit):
        cli._check_repair_args(args)


def test_no_repair_module_import_without_flag():
    import subprocess, sys
    code = ("import sys; from kefplayer import cli; "
            "cli.build_parser().parse_args(['play','x.flac']); "
            "print('kefplayer.repair_exec' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    assert out == "False"
