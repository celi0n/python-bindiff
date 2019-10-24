#from __future__ import annotations  #put it back when python 3.7 will be widely adopted
from __future__ import absolute_import
import sqlite3
import logging
import shutil
from datetime import datetime
import subprocess
import tempfile
from pathlib import Path
from typing import Union, Optional, Dict, List, Tuple

from binexport import ProgramBinExport

from bindiff import BINDIFF_BINARY
from bindiff.types import ProgramBinDiff, FunctionBinDiff, BasicBlockBinDiff, InstructionBinDiff
from bindiff.types import BasicBlockAlgorithm, FunctionAlgorithm


class BinDiff:
    """
    BinDiff class. Parse the diffing result of Bindiff and apply it to the two
    ProgramBinExport given. All the diff result is embedded in the two programs
    object so after loading the class can be dropped if needed.
    """

    def __init__(self, primary: Union[ProgramBinExport, str], secondary: Union[ProgramBinExport, str], diff_file: str):
        """
        BinDiff construct. Takes the two program and the diffing result file.
        Load the two programs if not given as ProgramBinExport object.
        .. warning:: the two programs given are mutated into ProgramBinDiff classes
        :param primary: first program diffed
        :param secondary: second program diffed
        :param diff_file: diffing file as generated by bindiff (differ more specifically)
        """
        self.primary = ProgramBinExport(primary) if isinstance(primary, str) else primary
        self.secondary = ProgramBinExport(secondary) if isinstance(secondary, str) else secondary
        self._convert_program_classes(self.primary)
        self._convert_program_classes(self.secondary)
        self.similarity = None
        self.confidence = None
        self.version = None
        self.created = None
        self.modified = None
        self.single_match = []

        conn = sqlite3.connect('file:'+diff_file+'?mode=ro', uri=True)
        self._load_metadata(conn.cursor())
        # also set the similarity/confidence in case the user want to drop the BinDiff object
        self.primary.similarity, self.secondary.similarity = self.similarity, self.similarity
        self.primary.confidence, self.secondary.confidence = self.confidence, self.confidence

        #Extract all the data from the database
        fun_query = "SELECT id, address1, address2, similarity, confidence, algorithm FROM function"
        funs = {x[0]: list(x[1:])+[{}] for x in conn.execute(fun_query)}
        query = "SELECT bb.functionid, bb.id, bb.address1, bb.address2, bb.algorithm, i.address1, i.address2 FROM " \
                "basicblock AS bb, instruction AS i WHERE bb.id == i.basicblockid"
        for f_id, bb_id, bb_addr1, bb_addr2, bb_algo, i1, i2 in conn.execute(query):
            if bb_id in funs[f_id][5]:
                funs[f_id][5][bb_id][3].append((i1, i2))
            else:
                funs[f_id][5][bb_id] = [bb_addr1, bb_addr2, bb_algo, [(i1, i2)]]

        for f_data in funs.values():
            self._load_function_info(*f_data)
        conn.close()

    def _convert_program_classes(self, p: ProgramBinExport) -> None:
        """
        Internal method to mutate a ProgramBinExport into ProgramBinDiff.
        :param p: program to mutate
        :return: None (perform all the side effect on the program)
        """
        p.__class__ = ProgramBinDiff
        for f in p.values():
            f.__class__ = FunctionBinDiff
            for bb in f.values():
                bb.__class__ = BasicBlockBinDiff
                for i in bb.values():
                    i.__class__ = InstructionBinDiff

    def _load_metadata(self, cursor: sqlite3.Cursor) -> None:
        """
        Load diffing metadata as stored in the DB file
        :param cursor: sqlite3 cursor to the DB
        :return: None
        """
        query = "SELECT created, modified, similarity, confidence FROM metadata"
        self.created, self.modified, self.similarity, self.confidence = cursor.execute(query).fetchone()
        self.created = datetime.strptime(self.created, "%Y-%m-%d %H:%M:%S")
        self.modified = datetime.strptime(self.modified, "%Y-%m-%d %H:%M:%S")
        self.similarity = float("{0:.3f}".format(self.similarity))  # round the value to 3 decimals
        self.confidence = float("{0:.3f}".format(self.confidence))  # round the value to 3 decimals

    def _load_function_info(self, addr1: int, addr2: int, similarity: float, confidence: float, algo: int, bbs_data: Dict[int, list]) -> None:
        """
        For the given db entry apply the matching info (and recursively apply it
        on basic blocks
        :param addr1: address of the function in primary
        :param addr2: address of the function in secondary
        :param similarity: similarity between the two functions
        :param confidence: similarity confidence between the two functions
        :param algo: algorithm that applied the match
        :param bbs_data: basic block datas
        :return: None
        """
        f1 = self.primary[addr1]
        f2 = self.secondary[addr2]
        f1.similarity, f2.similarity = similarity, similarity
        f1.confidence, f2.confidence = confidence, confidence
        f1.algorithm, f2.algorithm = FunctionAlgorithm(algo), FunctionAlgorithm(algo)
        f1.match, f2.match = f2, f1
        #print("f1: 0x%x, f2: 0x%x (id:%d)" % (f1.addr, f2.addr, f_id))
        for bb_data in bbs_data.values():
            self._load_basic_block_info(f1, f2, *bb_data)

    def _load_basic_block_info(self, f1: FunctionBinDiff, f2: FunctionBinDiff, bb_addr1: int, bb_addr2: int, algo: int,
                               inst_data: List[Tuple[int, int]]) -> None:
        """
        Load matching data for a basic block. At this point a basic block
        is "BinDiff" style. So we spread the matching on all "IDA" style the
        basic block. It is possible that a source basic block match multiple
        destination basic block. Thus to keep a 1-1 basic block matching put
        the conflicting instructions in single_match (to get rid of them).
        :param f1: current primary function
        :param f2: current secondary function
        :param bb_addr1: current basic block to match (in primary)
        :param bb_addr2: current basic block to match (in secondary)
        :param algo: algorithm that matched
        :param inst_data: insturction data
        :return: None
        """
        #print("bbid:%d bb_addr1:0x%x bb_addr:0x%x" % (bb_id, bb_addr1, bb_addr2))
        while inst_data:
            bb1, bb2 = f1[bb_addr1], f2[bb_addr2]
            if bb1.match or bb2.match:
                if bb1.match != bb2 or bb2.match != bb1:
                    print("Will make a basic block to match another one: (0x%x-0x%x) (0x%x-0x%x)" % (bb1.addr, bb1.match.addr, bb2.addr, bb2.match.addr))
            bb1.match, bb2.match = bb2, bb1
            bb1.algorithm, bb2.algorithm = BasicBlockAlgorithm(algo), BasicBlockAlgorithm(algo)
            while inst_data:
                i_addr1, i_addr2 = inst_data.pop(0)
                try:
                    self._load_instruction_info(bb1[i_addr1], bb2[i_addr2])
                except KeyError as e:
                    # Both instruction should be in a new unmatched basic blocks (other make them orphan)
                    if i_addr1 not in bb1 and i_addr2 not in bb2:
                        bb_addr1 = i_addr1 if i_addr1 in f1 else [x.addr for x in f1.values() if i_addr1 in x][0]
                        bb_addr2 = i_addr2 if i_addr2 in f2 else [x.addr for x in f2.values() if i_addr2 in x][0]
                        if f1[bb_addr1].match or f2[bb_addr2].match:
                            print("One of the two block is already matched")
                            self.single_match.append((i_addr1, i_addr2))
                        else:  # else put instructions back in the list
                            inst_data.insert(0, (i_addr1, i_addr2))
                    else:
                        self.single_match.append((i_addr1, i_addr2))
                    break

    def _load_instruction_info(self, inst1: int, inst2: int) -> None:
        """
        Set the match between two instructions
        :param inst1: instruction address in primary
        :param inst2: instruction address in secondary
        :return: None
        """
        inst1.match, inst2.match = inst2, inst1

    @staticmethod
    def _start_diffing(p1_path: str, p2_path: str, out_diff: str) -> int:
        """
        Static method to diff two binexport files against each other and storing
        the diffing result in the given file
        :param p1_path: primary file path
        :param p2_path: secondary file path
        :param out_diff: diffing output file
        :return: int (0 if successfull, -x otherwise)
        """
        tmp_dir = Path(tempfile.mkdtemp())
        f1 = Path(p1_path)
        f2 = Path(p2_path)
        cmd_line = [BINDIFF_BINARY.as_posix(), '--primary=%s' % p1_path, '--secondary=%s' % p2_path,
                    '--output_dir=%s' % tmp_dir.as_posix()]
        process = subprocess.Popen(cmd_line, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        retcode = process.wait()
        if retcode != 0:
            logging.error("differ terminated with error code: %d" % retcode)
            return retcode
        # Now look for the generated file
        out_file = tmp_dir / "{}_vs_{}.BinDiff".format(f1.stem, f2.stem)
        if out_file.exists():
            shutil.move(out_file, out_diff)
        else:  # try iterating the directory to find the .BinExport file
            candidates = list(tmp_dir.iterdir())
            if len(candidates) > 1:
                logging.warning("the output directory not meant to contain multiple files")
            found = False
            for file in candidates:
                if file.suffix == ".BinExport":
                    shutil.move(file, out_diff)
                    found = True
                    break
            if not found:
                logging.error("diff file .BinExport not found")
                return -2
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return 0

    @staticmethod
    def from_binary_files(p1_path: str, p2_path: str, diff_out: str) -> Optional['BinDiff']:
        """
        Diff two executable files. Thus it export .BinExport files from IDA
        and then diff the two resulting files in BinDiff.
        :param p1_path: primary binary file to diff
        :param p2_path: secondary binary file to diff
        :param diff_out: output file for the diff
        :return: BinDiff object representing the diff
        """
        p1 = ProgramBinExport.from_binary_file(p1_path)
        p2 = ProgramBinExport.from_binary_file(p2_path)
        p1_binexport = Path(p1_path).with_suffix(".BinExport")
        p2_binexport = Path(p2_path).with_suffix(".BinExport")
        if p1 and p2:
            retcode = BinDiff._start_diffing(p1_binexport, p2_binexport, diff_out)
            return BinDiff(p1, p2, diff_out) if retcode == 0 else None
        else:
            logging.error("p1 or p2 could not have been 'binexported'")
            return None

    @staticmethod
    def from_binexport_files(p1_binexport: str, p2_binexport: str, diff_out: str) -> Optional['BinDiff']:
        """
        Diff two binexport files. Diff the two binexport files with bindiff
        and then load a BinDiff instance.
        :param p1_binexport: primary binexport file to diff
        :param p2_binexport: secondary binexport file to diff
        :param diff_out: output file for the diff
        :return: BinDiff object representing the diff
        """
        retcode = BinDiff._start_diffing(p1_binexport, p2_binexport, diff_out)
        return BinDiff(p1_binexport, p2_binexport, diff_out) if retcode == 0 else None
