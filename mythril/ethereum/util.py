"""This module contains various utility functions regarding unit conversion and
solc integration."""

import binascii
import json
import logging
import os
import platform
import typing
from json.decoder import JSONDecodeError
from subprocess import PIPE, Popen
from typing import Tuple

import semantic_version as semver
import solcx
from pyparsing import Combine, Optional, Regex, Word
from requests.exceptions import ConnectionError

from mythril.exceptions import CompilerError
from mythril.support.support_args import args

log = logging.getLogger(__name__)


def safe_decode(hex_encoded_string):
    """

    :param hex_encoded_string:
    :return:
    """
    if hex_encoded_string.startswith("0x"):
        return bytes.fromhex(hex_encoded_string[2:])
    else:
        return bytes.fromhex(hex_encoded_string)


def get_solc_json(file, solc_binary="solc", solc_settings_json=None):
    """

    :param file:
    :param solc_binary:
    :param solc_settings_json:
    :return:
    """
    if args.solc_args is None:
        cmd = [solc_binary, "--standard-json", "--allow-paths", ".,/"]
    else:
        cmd = [solc_binary, "--standard-json"] + args.solc_args.split()

    settings = {}
    if solc_settings_json:
        with open(solc_settings_json) as f:
            settings = json.load(f)
    if "optimizer" not in settings:
        settings.update({"optimizer": {"enabled": False}})

    settings.update(
        {
            "outputSelection": {
                "*": {
                    "": ["ast"],
                    "*": [
                        "metadata",
                        "evm.bytecode",
                        "evm.deployedBytecode",
                        "evm.methodIdentifiers",
                    ],
                }
            },
        }
    )

    input_json = json.dumps(
        {
            "language": "Solidity",
            "sources": {file: {"urls": [file]}},
            "settings": settings,
        }
    )

    try:
        p = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE)
        stdout, stderr = p.communicate(bytes(input_json, "utf8"))

    except FileNotFoundError:
        raise CompilerError(
            "Compiler not found. Make sure that solc is installed and in PATH, or set the SOLC environment variable."
        )

    out = stdout.decode("UTF-8")

    try:
        result = json.loads(out)
    except JSONDecodeError as e:
        log.error(f"Encountered a decode error.\n stdout:{out}\n stderr: {stderr}")
        raise e

    for error in result.get("errors", []):
        if error["severity"] == "error":
            raise CompilerError(
                "Solc experienced a fatal error.\n\n%s" % error["formattedMessage"]
            )

    return result


def get_random_address():
    """

    :return:
    """
    return binascii.b2a_hex(os.urandom(20)).decode("UTF-8")


def get_indexed_address(index):
    """

    :param index:
    :return:
    """
    return "0x" + (hex(index)[2:] * 40)


def solc_exists(version):
    """

    :param version:
    :return:
    """

    if platform.system() == "Darwin":
        solcx.import_installed_solc()
    solcx.install_solc("v" + version)
    solcx.set_solc_version("v" + version)
    solc_binary = solcx.install.get_executable()
    return solc_binary


def parse_pragma(solidity_code):
    lt = Word("<")
    gtr = Word(">")
    eq = Word("=")
    carrot = Word("^")
    version = Regex(r"\s*[0-9]+\s*\.\s*[0-9]+\s*(\.\s*[0-9]+)?")
    inequality = Optional(
        eq | (Combine(gtr + Optional(eq)) | Combine(lt + Optional(eq)))
    )
    min_version = Optional(carrot | inequality) + version
    max_version = Optional(inequality + version)
    pragma = Word("pragma") + Word("solidity") + min_version + Optional(max_version)
    result = pragma.parseString(solidity_code)
    min_inequality = result[2] if result[2] in [">", "<", ">=", "<=", "="] else ""
    min_carrot = result[2] if result[2] == "^" else ""
    min_version = result[3] if min_carrot != "" or min_inequality != "" else result[2]
    return {
        "min_carrot": min_carrot,
        "min_inequality": min_inequality,
        "min_version": min_version,
        "max_inequality": result[4] if len(result) > 4 else None,
        "max_version": result[5] if len(result) > 5 else None,
    }


try:
    all_versions = solcx.get_installable_solc_versions()
except ConnectionError:
    # No internet, trying to proceed with installed compilers
    all_versions = solcx.get_installed_solc_versions()


# remove_comments_strings(prg):
# Return Solidity program prg without comments and strings
# [SmartBugs, https://github.com/smartbugs/smartbugs/blob/master/sb/solidity.py]
import re

VOID_START = re.compile("//|/\\*|\"|'")
QUOTE_END = re.compile("(?<!\\\\)'")
DQUOTE_END = re.compile('(?<!\\\\)"')

def remove_comments_strings(prg):
    todo = "\n".join(prg) # normalize line ends
    done = ""
    while True:
        m = VOID_START.search(todo)
        if not m:
            done += todo
            break
        else:
            done += todo[:m.start()]
            if m[0] == "//":
                end = todo.find('\n', m.end())
                todo = "" if end == -1 else todo[end:]
            elif m[0] == "/*":
                end = todo.find("*/", m.end())
                done += " "
                todo = "" if end == -1 else todo[end+2:]
            else:
                if m[0] == "'":
                    m2 = QUOTE_END.search(todo[m.end():])
                else:
                    m2 = DQUOTE_END.search(todo[m.end():])
                if not m2:
                    # unclosed string
                    break
                todo = todo[m.end()+m2.end():]
    return done


def extract_version(file: typing.Optional[str]):
    if file is None:
        return None
    version_line = None
    file = remove_comments_strings(file.split("\n"))
    for line in file.split("\n"):
        if "pragma solidity" not in line:
            continue
        version_line = line.rstrip()
        break
    if version_line is None:
        return None

    assert "pragma solidity" in version_line
    if version_line[-1] == ";":
        version_line = version_line[:-1]
    version_line = version_line[version_line.find("pragma") :]
    pragma_dict = parse_pragma(version_line)

    min_inequality = pragma_dict.get("min_inequality", None)
    max_inequality = pragma_dict.get("max_inequality", None)
    min_version = pragma_dict.get("min_version", None)
    if min_version is not None:
        min_version = min_version.replace(" ", "").replace("\t", "")
    max_version = pragma_dict.get("max_version", None)
    if max_version is not None:
        max_version = max_version.replace(" ", "").replace("\t", "")

    version_spec = (
        f"{min_inequality}{min_version},{max_inequality}{max_version}"
        if max_version
        else min_version
    )
    version_constraint = semver.SimpleSpec(version_spec)

    for version in all_versions:
        semver_version = semver.Version(str(version))
        if semver_version in version_constraint:
            if "0.5.17" in str(semver_version):
                # Solidity 0.5.17 Does not compile in a lot of cases.
                continue
            return str(semver_version)


def extract_binary(file: str) -> Tuple[str, str]:
    file_data = None
    with open(file) as f:
        file_data = f.read()

    version = extract_version(file_data)

    if version is None:
        return os.environ.get("SOLC") or "solc", version
    return solc_exists(version), version
