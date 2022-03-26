#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2021 Rot127 <unisono@quyllur.org>
#
# SPDX-License-Identifier: LGPL-3.0-only

import itertools
import json
import os
import re
import subprocess
import argparse

from HardwareRegister import HardwareRegister
from DuplexInstruction import DuplexInstruction, DuplexIClass
from ImplementationException import ImplementationException
from Instruction import Instruction
from SubInstruction import SubInstruction
from helperFunctions import (
    log,
    LogLevel,
    get_generation_warning_c_code,
    indent_code_block,
    unfold_llvm_sequence,
    get_include_guard,
    make_c_block,
    set_pos_after_license,
    get_license,
    get_generation_timestamp,
    compare_src_to_old_src,
)
import PluginInfo
import HexagonArchInfo


class LLVMImporter:
    config = dict()
    hexArch = dict()
    hexagon_target_json_path = ""
    llvm_instructions = dict()
    normal_instruction_names = list()
    normal_instructions = dict()
    sub_instruction_names = list()
    sub_instructions = dict()
    duplex_instructions_names = list()
    duplex_instructions = dict()
    hardware_regs = dict()

    def __init__(self, build_json: bool, test_mode=False):
        self.test_mode = test_mode
        if self.test_mode:
            self.hexagon_target_json_path = "../Hexagon.json"
        else:
            self.hexagon_target_json_path = "Hexagon.json"
        self.get_import_config()
        if build_json:
            self.generate_hexagon_json()
        else:
            if not os.path.exists(self.hexagon_target_json_path):
                log("No Hexagon.json found. Please check out the help message to generate it.", LogLevel.ERROR)
                exit()
            self.set_llvm_commit_info(use_prev=True)

        with open(self.hexagon_target_json_path) as file:
            self.hexArch = json.load(file)
        self.update_hex_arch()
        log("LLVM Hexagon target dump successfully loaded.")

        # Save types
        HexagonArchInfo.IMMEDIATE_TYPES = self.hexArch["!instanceof"]["Operand"]
        HexagonArchInfo.REG_CLASS_NAMES = self.hexArch["!instanceof"]["RegisterClass"]
        HexagonArchInfo.LLVM_FAKE_REGS = self.hexArch["!instanceof"]["HexagonFakeReg"]
        HexagonArchInfo.ALL_REG_NAMES = self.hexArch["!instanceof"]["DwarfRegNum"]
        HexagonArchInfo.CALLEE_SAVED_REGS = [name[0]["def"] for name in self.hexArch["HexagonCSR"]["SaveList"]["args"]]
        HexagonArchInfo.CC_REGS = self.get_cc_regs()

        self.unchanged_files = []  # Src files which had no changes after generation.

        # RIZIN SPECIFIC
        # Name of the function which parses the encoded register index bits.
        self.reg_resolve_decl = list()

        self.parse_hardware_registers()
        self.parse_instructions()
        self.generate_duplex_instructions()
        self.check_insn_syntax_length()
        if not test_mode:
            self.generate_rizin_code()
            self.generate_decompiler_code()
            self.add_license_info_header()
            self.apply_clang_format()
        log("Done")

    def get_import_config(self):
        """Loads the importer configuration from a file and writes it to self.config"""
        cwd = os.getcwd()
        log("Load LLVMImporter configuration from {}/.config".format(cwd))
        if cwd.split("/")[-1] == "rz-hexagon" or self.test_mode:
            self.config["GENERATOR_ROOT_DIR"] = cwd if not self.test_mode else "/".join(cwd.split("/")[:-1])
            if not os.path.exists(".config"):
                with open(cwd + "/.config", "w") as f:
                    config = "# Configuration for th LLVMImporter.\n"
                    config += "LLVM_PROJECT_REPO_DIR = /path/to/llvm_project"
                    f.write(config)
                log(
                    "This is your first time running the generator{}.".format(" TESTS" if self.test_mode else "")
                    + " Please set the path to the llvm_project repo in {}/.config.".format(cwd)
                )
                exit()
            with open(cwd + "/.config") as f:
                for line in f.readlines():
                    ln = line.strip()
                    if ln[0] == "#":
                        continue
                    ln = ln.split("=")
                    if ln[0].strip() == "LLVM_PROJECT_REPO_DIR":
                        dr = ln[1].strip()
                        if not os.path.exists(dr):
                            log(
                                "The LLVM_PROJECT_REPO_DIR is set to an invalid directory: '{}'".format(dr),
                                LogLevel.ERROR,
                            )
                            exit()
                        self.config["LLVM_PROJECT_REPO_DIR"] = dr
                        self.config["LLVM_PROJECT_HEXAGON_DIR"] = dr + "/llvm/lib/Target/Hexagon"
                    else:
                        log("Unknown configuration in config file: '{}'".format(ln[0]), LogLevel.WARNING)
        else:
            log("Please execute this script in the rz-hexagon directory.", LogLevel.ERROR)
            exit()

    def set_llvm_commit_info(self, use_prev: bool):
        """Writes the LLVM commit hash and commit date to self.config.

        :param use_prev: If True it uses the information when Hexagon.json was generated previously.
            False: it gets the date of the current checked out LLVM commit.
        """

        if not use_prev:
            self.config["LLVM_COMMIT_DATE"] = (
                subprocess.check_output(
                    ["git", "show", "-s", "--format=%ci", "HEAD"], cwd=self.config["LLVM_PROJECT_REPO_DIR"]
                )
                .decode("ascii")
                .strip("\n")
            )
            self.config["LLVM_COMMIT_DATE"] += " (ISO 8601 format)"
            self.config["LLVM_COMMIT_HASH"] = (
                subprocess.check_output(
                    ["git", "show", "-s", "--format=%H", "HEAD"], cwd=self.config["LLVM_PROJECT_REPO_DIR"]
                )
                .decode("ascii")
                .strip("\n")
            )
            with open(".last_llvm_commit_info", "w") as f:
                f.write(self.config["LLVM_COMMIT_DATE"])
                f.write("\n")
                f.write(self.config["LLVM_COMMIT_HASH"])
        else:
            if os.path.exists(".last_llvm_commit_info"):
                with open(".last_llvm_commit_info", "r") as f:
                    self.config["LLVM_COMMIT_DATE"] = str(f.readline()).strip()
                    self.config["LLVM_COMMIT_HASH"] = str(f.readline()).strip()
            else:
                log("No previous LLVM commit info found.", LogLevel.VERBOSE if self.test_mode else LogLevel.WARNING)
                self.config["LLVM_COMMIT_DATE"] = "Test" if self.test_mode else "None"
                self.config["LLVM_COMMIT_HASH"] = "Test" if self.test_mode else "None"

    def generate_hexagon_json(self):
        """Generates the Hexagon.json file with LLVMs tablegen."""

        log("Generate Hexagon.json from LLVM target descriptions.")
        self.set_llvm_commit_info(use_prev=False)
        subprocess.call(
            [
                "llvm-tblgen",
                "-I",
                "../../../include/",
                "--dump-json",
                "-o",
                "{}/Hexagon.json".format(self.config["GENERATOR_ROOT_DIR"]),
                "Hexagon.td",
            ],
            cwd=self.config["LLVM_PROJECT_HEXAGON_DIR"],
        )

    def update_hex_arch(self):
        """Imports system instructions and registers described in the manual but not implemented by LLVM."""
        reg_count = 0
        self.hexArch["!instanceof"]["RegisterClass"] += [
            "SysRegs",
            "SysRegs64",
        ]
        reg_dir = "./import/registers/" if not self.test_mode else "../import/registers/"
        for filename in os.listdir(reg_dir):
            if filename.split(".")[-1] != "json":
                continue
            with open(reg_dir + filename) as f:
                reg = json.load(f)
            reg_name = list(reg.keys())[0]
            if reg_name != "SysRegs" or reg_name != "SysRegs64":
                if reg_name in self.hexArch["!instanceof"]["DwarfRegNum"]:
                    raise ImplementationException(
                        "Register {} already present in the LLVM definitions."
                        " Please check whether LLVM implements System/Monitor"
                        " instructions and system registers etc.".format(reg_name)
                    )
                self.hexArch["!instanceof"]["DwarfRegNum"] += reg.keys()
                reg_count += 1
            self.hexArch.update(reg)

        instr_count = 0
        insn_dir = "./import/instructions/" if not self.test_mode else "../import/instructions/"
        for filename in os.listdir(insn_dir):
            if filename.split(".")[-1] != "json":
                continue
            instn_name = filename.replace(".json", "")
            with open(insn_dir + filename) as f:
                insn = json.load(f)
            syntax_list = list()
            for llvm_instr in self.hexArch["!instanceof"]["HInst"]:
                syntax_list.append(self.hexArch[llvm_instr]["AsmString"])
            if "UNDOCUMENTED" not in instn_name and insn[instn_name]["AsmString"] in syntax_list:
                continue
            self.hexArch.update(insn)
            self.hexArch["!instanceof"]["HInst"] += list(insn.keys())
            instr_count += 1
        log("Imported {} registers.".format(reg_count))
        log("Imported {} instructions.".format(instr_count))

    def parse_instructions(self) -> None:
        for i, i_name in enumerate(self.hexArch["!instanceof"]["HInst"]):
            llvm_instruction = self.hexArch[i_name]
            if llvm_instruction is None:
                log(
                    "Could not find instruction with name: {} in json file.".format(i_name),
                    LogLevel.ERROR,
                )
                continue
            if llvm_instruction["isPseudo"]:
                log(
                    "Pseudo instruction passed. Name: {}".format(i_name),
                    LogLevel.VERBOSE,
                )
                continue
            log("{} | Parse {}".format(i, i_name), LogLevel.VERBOSE)
            self.llvm_instructions[i_name] = llvm_instruction

            if llvm_instruction["Type"]["def"] == "TypeSUBINSN":
                self.sub_instruction_names.append(i_name)
                self.sub_instructions[i_name] = SubInstruction(llvm_instruction)
                # log(i_name, LogLevel.DEBUG)
            else:
                self.normal_instruction_names.append(i_name)
                self.normal_instructions[i_name] = Instruction(llvm_instruction)

        log("Parsed {} normal instructions.".format(len(self.normal_instructions)))
        log("Parsed {} sub-instructions.".format(len(self.sub_instructions)))

    def generate_duplex_instructions(self) -> None:
        sub_instr_pairs = itertools.product(self.sub_instructions.values(), self.sub_instructions.values())
        for pair in sub_instr_pairs:
            low_instr = pair[0]
            high_instr = pair[1]
            i_class = DuplexInstruction.get_duplex_i_class_of_instr_pair(low=low_instr, high=high_instr)
            if i_class != DuplexIClass.INVALID and DuplexInstruction.fulfill_constraints(low_instr, high_instr):
                llvm_dup_instr = self.hexArch[i_class.name]
                dup_instr = DuplexInstruction(
                    llvm_duplex_instr=llvm_dup_instr,
                    low=low_instr,
                    high=high_instr,
                )
                self.duplex_instructions[dup_instr.name] = dup_instr
                self.duplex_instructions_names.append(dup_instr.name)
                # log("Duplex instruction generated: {}".format(dup_instr.name), LogLevel.DEBUG)
        log("Generated {} duplex instructions.".format(len(self.duplex_instructions)))

    def parse_hardware_registers(self) -> None:
        cc = 0
        cr = 0
        for reg_class_name in HexagonArchInfo.REG_CLASS_NAMES:
            # LLVM fake register class; VectRegRev = reverse double register: V0:1 instead of V1:0
            if reg_class_name == "UsrBits" or reg_class_name == "VectRegRev":
                continue
            # Register class which holds all new register of an arch version. Irrelevant for us at the moment.
            if reg_class_name == "V65Regs" or reg_class_name == "V62Regs":
                continue

            self.hardware_regs[reg_class_name] = dict()
            reg_class: dict = self.hexArch[reg_class_name]
            # Use "Alignment" although a "Size" attribute exists. But Double Regs set that to 0.
            size: int = reg_class["Alignment"] if reg_class["Size"] == 0 else reg_class["Size"]

            reg_names = list()
            for a in reg_class["MemberList"]["args"]:
                arg = a[0]
                if "def" in arg:
                    # VTMP = LLVM fake register
                    if arg["def"] == "VTMP":
                        continue
                    if reg_class_name == "CtrRegs" and arg["def"] == "C8":
                        # For whatever reason this C8 occurs twice, but as USR reg.
                        # We better use the USR reg as it lists c8 als alternative name for itself.
                        continue
                    reg_names.append(arg["def"])
                elif "sequence" in arg["printable"]:
                    reg_names = reg_names + unfold_llvm_sequence(arg["printable"])
                # Remove registers whichs tart with WR; WR register are reverse double vector regs: V0:1 instead of V1:0
                # TODO This is not nice. Isn't there a simpler way?
                reg_names = [name for name in reg_names if not re.search(r"WR\d{1,2}", name)]

            for name in reg_names:
                llvm_reg = self.hexArch[name]
                reg = HardwareRegister(
                    llvm_reg_class=reg_class_name,
                    name=name,
                    llvm_object=llvm_reg,
                    size=size,
                )
                self.hardware_regs[reg_class_name][name] = reg
                cr += 1
                # log("Added reg: {}::{} with hw encoding: {}".format(name, reg_class_name,
                #                                                     reg.hw_encoding), LogLevel.DEBUG)

            cc += 1
        log("Parsed {} hardware registers of {} different register classes.".format(cr, cc))

    def check_insn_syntax_length(self):
        for instr_set in [
            self.normal_instructions.values(),
            self.duplex_instructions.values(),
        ]:
            for insn in instr_set:
                if len(insn.syntax) >= 128:
                    sl = len(insn.syntax) + 1  # +1 for \0 in the string
                    raise ImplementationException(
                        "The mnemonic variable is at the moment only 128 byte."
                        + "This syntax takes at least {}+1 bytes.".format(sl)
                    )

    def get_cc_regs(self) -> dict:
        """Returns a list of register names which are argument or return register in the calling convention.
        This part is a bit tricky. The register names are stored in objects named "anonymous_XXX" in Hexagon.json.
        Since they do not have a explicit name, we can only check check the names against the source.

        Note: LLVM defines the calling convention in: HexagonCallingConv.td

        Returns: dict = {"GPR_args":list[str], "GPR_ret":list[str], "HVX_args":list[str], "HVX_ret":list[str],}
        """
        cc_regs = dict()
        anon_obj_names = self.hexArch["!instanceof"]["CCAssignToReg"]
        arg_regs = [reg["def"] for reg in self.hexArch[anon_obj_names[0]]["RegList"]]  # Single
        arg_regs += [reg["def"] for reg in self.hexArch[anon_obj_names[1]]["RegList"]]  # Double
        cc_regs["GPR_args"] = arg_regs

        ret_regs = [reg["def"] for reg in self.hexArch[anon_obj_names[2]]["RegList"]]  # Single
        ret_regs += [reg["def"] for reg in self.hexArch[anon_obj_names[3]]["RegList"]]  # Double
        cc_regs["GPR_ret"] = ret_regs

        hvx_arg_regs = [reg["def"] for reg in self.hexArch[anon_obj_names[4]]["RegList"]]  # Single
        hvx_arg_regs += [reg["def"] for reg in self.hexArch[anon_obj_names[5]]["RegList"]]  # Double
        cc_regs["HVX_args"] = hvx_arg_regs

        hvx_ret_regs = [reg["def"] for reg in self.hexArch[anon_obj_names[6]]["RegList"]]  # Single
        hvx_ret_regs += [reg["def"] for reg in self.hexArch[anon_obj_names[7]]["RegList"]]  # Double
        cc_regs["HVX_ret"] = hvx_ret_regs

        return cc_regs

    # RIZIN SPECIFIC
    def generate_rizin_code(self) -> None:
        log("Generate and write source code.")
        self.build_hexagon_insn_enum_h()
        self.build_hexagon_disas_c()
        self.build_hexagon_c()
        self.build_hexagon_h()
        self.build_asm_hexagon_c()
        self.build_hexagon_arch_c()
        self.build_hexagon_arch_h()
        self.copy_tests()
        self.build_hexagon_regs()
        self.build_cc_hexagon_32_sdb_txt()

        # TODO hexagon.h: Gen - HexOpType, IClasses, Regs and its aliases (system = guest),
        #  + corresponding functions in hexagon.c: hex_get_sub_regpair etc.

    # RIZIN SPECIFIC
    # TODO Wouldn't it be a wonderful world...
    def generate_decompiler_code(self) -> None:
        pass

    # RIZIN SPECIFIC
    def add_license_info_header(self) -> None:
        log("Add license headers")
        for subdir, dirs, files in os.walk("./rizin/"):
            for file in files:
                if file == "hexagon" or file[-3:] == "txt":  # Tests
                    continue
                p = os.path.join(subdir, file)
                if p in self.unchanged_files:
                    continue
                with open(p, "r+") as f:
                    content = f.read()
                    f.seek(0, 0)
                    f.write(get_license() + "\n" + get_generation_timestamp(self.config) + "\n" + content)

    # RIZIN SPECIFIC
    def build_hexagon_insn_enum_h(self, path: str = "./rizin/librz/asm/arch/hexagon/hexagon_insn.h") -> None:
        data = get_generation_warning_c_code()
        data += "\n"
        data += get_include_guard("hexagon_insn.h")
        data += "\n"
        data += "enum HEX_INS {\n"
        enum = ""
        for name in self.normal_instruction_names + self.duplex_instructions_names:
            if "invalid_decode" in name:
                enum = (PluginInfo.INSTR_ENUM_PREFIX + name.upper() + " = 0,\n") + enum
            else:
                enum += PluginInfo.INSTR_ENUM_PREFIX + name.upper() + ",\n"
        data += enum
        data += "};\n\n"
        data += "#endif"
        if compare_src_to_old_src(data, path):
            self.unchanged_files.append(path)
            return

        with open(path, "w+") as dest:
            dest.writelines(data)
            log(
                "Hexagon instruction enum written to: {}".format(path),
                LogLevel.INFO,
            )

    # RIZIN SPECIFIC
    def build_hexagon_disas_c(self, path: str = "./rizin/librz/asm/arch/hexagon/hexagon_disas.c") -> None:
        # TODO Clean up this method
        indent = PluginInfo.LINE_INDENT
        var = PluginInfo.HEX_INSTR_VAR_SYNTAX
        signed_imm_array = "signed_imm[{}][32]".format(PluginInfo.MAX_OPERANDS)
        with open(path, "w+") as dest:
            dest.write(get_generation_warning_c_code())

            with open("handwritten/hexagon_disas_c/include.c") as include:
                set_pos_after_license(include)
                dest.writelines(include.readlines())

            with open("handwritten/hexagon_disas_c/functions.c") as functions:
                set_pos_after_license(functions)
                dest.writelines(functions.readlines())

            main_function = (
                "int hexagon_disasm_instruction(const RzAsm *rz_asm, HexState"
                " *state, const ut32 hi_u32, RZ_INOUT HexInsn *hi, HexPkt"
                " *pkt) {\n" + "ut32 addr = hi->addr;\n"
            )

            main_function += "if (hi->pkt_info.last_insn) {"
            main_function += "switch (hex_get_loop_flag(pkt)) {" + "default: break;"
            main_function += (
                "case HEX_LOOP_01:"
                + "hi->ana_op.prefix = RZ_ANALYSIS_OP_PREFIX_HWLOOP_END;"
                + "hi->ana_op.fail = pkt->hw_loop0_addr;"
                + "hi->ana_op.jump = pkt->hw_loop1_addr;"
                + "hi->ana_op.val = hi->ana_op.jump;"
                + "break;\n"
            )
            main_function += (
                "case HEX_LOOP_0:\n"
                + "hi->ana_op.prefix = RZ_ANALYSIS_OP_PREFIX_HWLOOP_END;"
                + "hi->ana_op.jump = pkt->hw_loop0_addr;"
                + "hi->ana_op.val = hi->ana_op.jump;"
                + "break;\n"
            )
            main_function += (
                "case HEX_LOOP_1:\n"
                + "hi->ana_op.prefix = RZ_ANALYSIS_OP_PREFIX_HWLOOP_END;"
                + "hi->ana_op.jump = pkt->hw_loop1_addr;"
                + "hi->ana_op.val = hi->ana_op.jump;"
                + "break;"
            )
            main_function += "}}"

            main_function += (
                "if (hi_u32 != 0x00000000) {\n"
                + "// DUPLEXES\n"
                + "if ((({} >> 14) & 0x3) == 0) {{\n".format(var)
                + "switch (((({} >> 29) & 0xF) << 1) | (({} >> 13) & 1)) {{\n".format(var, var)
            )

            # Duplexes
            for c in range(0xF):  # Class 0xf is reserved yet.
                main_function += "{}case 0x{:x}:\n".format(indent * 3, c)
                main_function += "hexagon_disasm_duplex_0x{:x}(rz_asm, state, hi_u32, hi," " addr, pkt);\n".format(c)
                func_body = ""
                func_header = (
                    "void hexagon_disasm_duplex_0x{:x}(const RzAsm *rz_asm,"
                    " HexState *state, const ut32 hi_u32, HexInsn *hi, const"
                    " ut32 addr, HexPkt *pkt) {{\n".format(c)
                )
                for d_instr in self.duplex_instructions.values():
                    if d_instr.encoding.get_i_class() == c:
                        func_body += indent_code_block(d_instr.get_instruction_init_in_c(), 1)
                        if "sprintf(signed_imm" in func_body and signed_imm_array not in func_header:
                            func_header += "char " + signed_imm_array + " = {0};\n"
                dest.write(func_header + func_body + "}\n\n")
                main_function += "{}break;\n".format(indent * 4)

            # Normal instructions
            # Brackets for switch, if
            main_function += "}\n}\nelse {\n"
            main_function += "switch (({} >> 28) & 0xF) {{\n".format(var)
            for c in range(0x10):
                main_function += "case 0x{:x}:\n".format(c)
                main_function += "hexagon_disasm_0x{:x}(rz_asm, state, hi_u32, hi, addr," " pkt);\n".format(c)

                func_body = ""
                func_header = (
                    "void hexagon_disasm_0x{:x}(const RzAsm *rz_asm, HexState"
                    " *state, const ut32 hi_u32, HexInsn *hi, const ut32 addr,"
                    " HexPkt *pkt) {{\n".format(c)
                )
                for instr in self.normal_instructions.values():
                    if instr.encoding.get_i_class() == c:
                        func_body += indent_code_block(instr.get_instruction_init_in_c(), 1)
                        if "sprintf(signed_imm" in func_body and signed_imm_array not in func_header:
                            func_header += "char " + signed_imm_array + " = {0};\n"
                dest.write(func_header + func_body + "}\n\n")
                main_function += "{}break;\n".format(indent * 4)

            # Closing brackets for switch, else, function
            main_function += "}\n}\n}"
            main_function += "if (pkt->is_eob && is_last_instr(hi->parse_bits)) {" + "hi->ana_op.eob = true;}"
            main_function += (
                "if (hi->instruction == HEX_INS_INVALID_DECODE) {\n"
                + "hi->parse_bits = ((hi_u32) & 0xc000) >> 14;\n"
                + "hi->ana_op.type = RZ_ANALYSIS_OP_TYPE_ILL;\n"
                + 'sprintf(hi->mnem_infix, "invalid");\n'
                + 'sprintf(hi->mnem, "%s%s%s", hi->pkt_info.mnem_prefix,'
                " hi->mnem_infix, hi->pkt_info.mnem_postfix);\n" + "}\n" + "return 4;\n}"
            )
            dest.write(main_function)
        log(
            "Hexagon instruction disassembler code written to: {}".format(path),
            LogLevel.DEBUG,
        )

    # RIZIN SPECIFIC
    def build_hexagon_h(self, path: str = "./rizin/librz/asm/arch/hexagon/hexagon.h") -> None:
        indent = PluginInfo.LINE_INDENT
        general_prefix = PluginInfo.GENERAL_ENUM_PREFIX

        code = get_generation_warning_c_code()
        code += "\n"
        code += get_include_guard("hexagon.h")

        with open("handwritten/hexagon_h/typedefs.h") as typedefs:
            set_pos_after_license(typedefs)
            code += "".join(typedefs.readlines())

        reg_class: str
        for reg_class in self.hardware_regs:
            code += "\ntypedef enum {\n"

            hw_reg: HardwareRegister
            for hw_reg in sorted(
                self.hardware_regs[reg_class].values(),
                key=lambda x: x.hw_encoding,
            ):
                alias = ",".join(hw_reg.alias)
                code += "{}{} = {},{}\n".format(
                        indent,
                        hw_reg.enum_name,
                        hw_reg.hw_encoding,
                        " // " + alias if alias != "" else "",
                    )
            code += "}} {}{}; // {}\n".format(
                    general_prefix,
                    HardwareRegister.register_class_name_to_upper(reg_class),
                    reg_class,
                )

        with open("handwritten/hexagon_h/macros.h") as macros:
            set_pos_after_license(macros)
            code += "".join(macros.readlines())
        code += "\n"
        if len(self.reg_resolve_decl) == 0:
            raise ImplementationException(
                "Register resolve declarations missing"
                "(They get generated together with hexagon.c)."
                "Please generate hexagon.c before hexagon.h"
            )
        for decl in self.reg_resolve_decl:
            code += decl + "\n"
        with open("handwritten/hexagon_h/declarations.h") as decl:
            set_pos_after_license(decl)
            code += "".join(decl.readlines())
        code += "#endif"

        if compare_src_to_old_src(code, path):
            self.unchanged_files.append(path)
            return

        with open(path, "w+") as dest:
            dest.writelines(code)
            log("hexagon.h written to: {}".format(path), LogLevel.INFO)

    # RIZIN SPECIFIC
    def build_hexagon_c(self, path: str = "./rizin/librz/asm/arch/hexagon/hexagon.c") -> None:
        code = get_generation_warning_c_code()
        with open("handwritten/hexagon_c/include.c") as include:
            set_pos_after_license(include)
            code += "".join(include.readlines())

        reg_class: str
        for reg_class in self.hardware_regs:
            func_name = HardwareRegister.get_func_name_of_class(reg_class, False)
            function = "char* {}(int opcode_reg)".format(func_name)
            self.reg_resolve_decl.append(function + ";")
            code += "\n{} {{\n".format(function)

            parsing_code = HardwareRegister.get_parse_code_reg_bits(reg_class, "opcode_reg")
            if parsing_code != "":
                code += "{}\n".format(parsing_code)

            code += "switch (opcode_reg) {\n"
            code += 'default:\nreturn "<err>";\n'

            hw_reg: HardwareRegister
            for hw_reg in self.hardware_regs[reg_class].values():
                code += 'case {}:\nreturn "{}";\n'.format(
                        hw_reg.enum_name,
                        hw_reg.asm_name.upper(),
                    )
            code += "}\n}\n"

        with open("handwritten/hexagon_c/functions.c") as func:
            set_pos_after_license(func)
            code += "".join(func.readlines())
        code += "\n"

        if compare_src_to_old_src(code, path):
            self.unchanged_files.append(path)
            return

        with open(path, "w+") as dest:
            dest.writelines(code)
            log("hexagon.c written to: {}".format(path), LogLevel.INFO)

    # RIZIN SPECIFIC
    def build_asm_hexagon_c(self, path: str = "./rizin/librz/asm/p/asm_hexagon.c") -> None:
        code = get_generation_warning_c_code()

        with open("handwritten/asm_hexagon_c/include.c") as include:
            set_pos_after_license(include)
            code += "".join(include.readlines())
        with open("handwritten/asm_hexagon_c/initialization.c") as init:
            set_pos_after_license(init)
            code += "".join(init.readlines())

        if compare_src_to_old_src(code, path):
            self.unchanged_files.append(path)
            return
        with open(path, "w+") as dest:
            dest.writelines(code)
            log("asm_hexagon.c written to {}".format(path), LogLevel.INFO)

    # RIZIN SPECIFIC
    def build_hexagon_arch_c(self, path: str = "./rizin/librz/asm/arch/hexagon/hexagon_arch.c"):
        code = get_generation_warning_c_code()

        with open("handwritten/hexagon_arch_c/include.c") as include:
            set_pos_after_license(include)
            code += "".join(include.readlines())
        with open("handwritten/hexagon_arch_c/functions.c") as functions:
            set_pos_after_license(functions)
            code += "".join(functions.readlines())

        if compare_src_to_old_src(code, path):
            self.unchanged_files.append(path)
            return
        with open(path, "w+") as dest:
            dest.writelines(code)
            log("asm_hexagon.c written to {}".format(path), LogLevel.INFO)

    # RIZIN SPECIFIC
    def build_hexagon_arch_h(self, path: str = "./rizin/librz/asm/arch/hexagon/hexagon_arch.h"):
        code = get_generation_warning_c_code()
        code += get_include_guard("hexagon_arch.h")

        with open("handwritten/hexagon_arch_h/includes.h") as includes:
            set_pos_after_license(includes)
            code += "".join(includes.readlines())
        with open("handwritten/hexagon_arch_h/typedefs.h") as typedefs:
            set_pos_after_license(typedefs)
            code += "".join(typedefs.readlines())
        with open("handwritten/hexagon_arch_h/declarations.h") as declarations:
            set_pos_after_license(declarations)
            code += "".join(declarations.readlines())
        code += "\n#endif\n"

        if compare_src_to_old_src(code, path):
            self.unchanged_files.append(path)
            return
        with open(path, "w+") as dest:
            dest.writelines(code)
            log("hexagon_arch.h written to {}".format(path), LogLevel.INFO)

    # RIZIN SPECIFIC
    @staticmethod
    def copy_tests() -> None:
        with open("handwritten/analysis-tests/hexagon") as f:
            with open("./rizin/test/db/analysis/hexagon", "w+") as g:
                set_pos_after_license(g)
                g.writelines(f.readlines())

        with open("handwritten/asm-tests/hexagon") as f:
            with open("./rizin/test/db/asm/hexagon", "w+") as g:
                set_pos_after_license(g)
                g.writelines(f.readlines())
        log("Copied test files to ./rizin/test/db/", LogLevel.DEBUG)

    # RIZIN SPECIFIC
    def build_hexagon_regs(self, path: str = "rizin/librz/analysis/p/analysis_hexagon.c") -> None:
        profile = self.get_alias_profile().splitlines(keepends=True)
        offset = 0

        for hw_reg_classes in self.hardware_regs:
            if hw_reg_classes in [
                "IntRegsLow8",
                "GeneralSubRegs",
                "GeneralDoubleLow8Regs",
                "ModRegs",
            ]:
                continue  # Those registers would only be duplicates.
            hw_reg: HardwareRegister
            for hw_reg in self.hardware_regs[hw_reg_classes].values():
                profile.append(hw_reg.get_reg_profile(offset) + "\n")
                offset += int((hw_reg.size / 8))
            profile.append("\n")
        profile = profile[:-1]  # Remove line breaks
        profile[-1] = profile[-1][:-1] + ";\n"  # [:-1] to remove line break.

        with open(path, "w+") as f:
            f.write(get_generation_warning_c_code())

            with open("handwritten/analysis_hexagon_c/include.c") as include:
                set_pos_after_license(include)
                f.writelines(include.readlines())
            with open("handwritten/analysis_hexagon_c/functions.c") as functions:
                set_pos_after_license(functions)
                f.writelines(functions.readlines())
            f.write("\n")

            tmp = list()
            tmp.append("const char *p =\n")
            tmp += profile
            tmp = make_c_block(
                lines=tmp,
                begin="RZ_API char *get_reg_profile(RzAnalysis *analysis)",
                ret="return strdup(p);\n",
            )
            f.writelines(tmp)
            f.write("\n")

            with open("handwritten/analysis_hexagon_c/initialization.c") as initialization:
                set_pos_after_license(initialization)
                f.writelines(initialization.readlines())
            f.write("\n")

    # RIZIN SPECIFC
    def get_alias_profile(self) -> str:
        """Returns the alias profile of register. A0 = R0, SP = R29 PC = pc etc."""
        indent = PluginInfo.LINE_INDENT

        p = "\n" + '"=PC{}pc\\n"'.format(indent) + "\n"
        p += '"=SP{}R29\\n"'.format(indent) + "\n"
        p += '"=BP{}R30\\n"'.format(indent) + "\n"
        p += '"=LR{}R31\\n"'.format(indent) + "\n"
        p += '"=SR{}usr\\n"'.format(indent) + "\n"
        p += '"=SN{}r0\\n"'.format(indent) + "\n"

        arg_regs = ""
        ret_regs = ""

        arguments = HexagonArchInfo.CC_REGS["GPR_args"]
        returns = HexagonArchInfo.CC_REGS["GPR_ret"]

        general_ps = list(self.hardware_regs["IntRegs"].values()) + list(self.hardware_regs["DoubleRegs"].values())
        gpr: HardwareRegister
        for gpr in general_ps:
            try:
                i = arguments.index(gpr.name)
            except ValueError:
                continue
            if i > 9 and gpr.name in HexagonArchInfo.CC_REGS["GPR_args"]:
                log(
                    "Can not add register {} as argument reg to the register"
                    " profile. ".format(gpr.name) + "Rizin only supports 10 argument registers. Check"
                    " rz_reg.h if this changed.",
                    LogLevel.WARNING,
                )
            if gpr.name in HexagonArchInfo.CC_REGS["GPR_args"]:
                arg_regs += '"=A{}{}{}\\n"'.format(i, indent, gpr.asm_name) + "\n"

        for gpr in general_ps:
            try:
                i = returns.index(gpr.name)
            except ValueError:
                continue
            if i > 3 and gpr.name in HexagonArchInfo.CC_REGS["GPR_ret"]:
                log(
                    "Can not add register {} as return reg to the register"
                    " profile. ".format(gpr.name) + "Rizin only supports 4 return registers. Check rz_reg.h"
                    " if this changed.",
                    LogLevel.WARNING,
                )
            if gpr.name in HexagonArchInfo.CC_REGS["GPR_ret"]:
                ret_regs += '"=R{}{}{}\\n"'.format(i, indent, gpr.asm_name) + "\n"

        p += arg_regs + ret_regs + "\n"

        return p

    # RIZIN SPECIFIC
    @staticmethod
    def build_cc_hexagon_32_sdb_txt(
        path: str = "rizin/librz/analysis/d/cc-hexagon-32.sdb.txt",
    ) -> None:
        """Builds the *incomplete* calling convention as sdb file.
        Hexagon can pass arguments and return values via different registers. E.g. either over R0 or R1:0.
        But the calling convention logic in rizin and the sdb is not sophisticated enough to model this.
        That is the reason we add only one of multiple possible argument/return register per db entry.
        """

        cc_dict = dict()
        with open(path, "w+") as f:
            for reg in HexagonArchInfo.CC_REGS["GPR_args"]:
                n = int(re.search(r"\d{1,2}", reg).group(0))
                if reg[0] == "R":
                    cc_dict["cc.hexagon.arg{}".format(n)] = "r{}".format(n)
                elif reg[0] == "D":
                    continue
                else:
                    raise ImplementationException(
                        "Could not assign register {} to a specific argument" " value.".format(reg)
                    )
            cc_dict["cc.hexagon.argn"] = "stack_rev"
            for reg in HexagonArchInfo.CC_REGS["GPR_ret"]:
                n = int(re.search(r"\d{1,2}", reg).group(0))
                if reg[0] == "R":
                    if HexagonArchInfo.CC_REGS["GPR_ret"].index(reg) == 0:
                        cc_dict["cc.hexagon.ret".format(n)] = "r{}".format(n)
                    else:
                        continue
                elif reg[0] == "D":
                    continue
                else:
                    raise ImplementationException(
                        "Could not assign register {} to a specific return" " value.".format(reg)
                    )

            f.write("default.cc=hexagon\n\nhexagon=cc\n")
            for k, v in cc_dict.items():
                f.write(k + "=" + v + "\n")
            f.write("\nhvx=cc\ncc.hvx.name=hvx\n")

            cc_dict = dict()
            for reg in HexagonArchInfo.CC_REGS["HVX_args"]:
                n = int(re.search(r"\d{1,2}", reg).group(0))
                if reg[0] == "V":
                    cc_dict["cc.hvx.arg{}".format(n)] = "v{}".format(n)
                elif reg[0] == "W":
                    continue
                else:
                    raise ImplementationException(
                        "Could not assign register {} to a specific argument" " value.".format(reg)
                    )
            for reg in HexagonArchInfo.CC_REGS["HVX_ret"]:
                n = int(re.search(r"\d{1,2}", reg).group(0))
                if reg[0] == "V":
                    if HexagonArchInfo.CC_REGS["HVX_ret"].index(reg) == 0:
                        cc_dict["cc.hvx.ret".format(n)] = "v{}".format(n)
                    else:
                        continue
                elif reg[0] == "W":
                    continue
                else:
                    raise ImplementationException(
                        "Could not assign register {} to a specific return" " value.".format(reg)
                    )
            for k, v in cc_dict.items():
                f.write(k + "=" + v + "\n")

    # RIZIN SPECIFIC
    @staticmethod
    def apply_clang_format() -> None:
        log("Apply clang-format.")
        for subdir, dirs, files in os.walk("rizin/librz/"):
            for file in files:
                p = os.path.join(subdir, file)
                if os.path.splitext(p)[-1] in [
                    ".c",
                    ".cpp",
                    ".h",
                    ".hpp",
                    ".inc",
                ]:
                    log("Format {}".format(p), LogLevel.VERBOSE)
                    os.system("clang-format-13 -style file -i " + p)


if __name__ == "__main__":
    parser = argparse.ArgumentParser("Import settings")
    parser.add_argument(
        "-j",
        action="store_true",
        default=False,
        help="Run llvm-tblgen to build a new Hexagon.json file from the LLVM definitons.",
        dest="bjs",
    )
    args = parser.parse_args()
    interface = LLVMImporter(args.bjs)
