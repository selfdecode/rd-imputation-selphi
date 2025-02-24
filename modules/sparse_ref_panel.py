"""
SparseReferencePanel is a class that stores reference genotypes
in chunked boolean sparse arrays. Chunking the arrays allows for
multi-threaded ingestion of genotype files. Loading the chunks
also provides faster loading times, lower memory usage, and faster
slicing times.

The reference panel is stored in a zstd-compressed zip archive:
    metadata - json file containing useful information about the sparse arrays
    variants - binary array of variants in panel
    chunks - binary array of chunks with start and stop positions
    haplotypes/* - chunks stored as boolean sparse npz files

Chunks are cached when they are read into memory. While a few seconds
processing time is required to load each chunk, consecutive reads are very fast.

Files can only contain one chromosome, and variants are assumed to be sorted by position.

To open xsi as sparse reference:
ref_panel = SparseReferencePanel("/data/30x/chr20.srp").from_xsi("/data/30x/chr20.xsi", threads=4)

To open vcf/bcf as sparse reference:
ref_panel = SparseReferencePanel("/data/30x/chr20.srp").from_bcf("/data/30x/chr20.bcf", threads=4)

To open existing sparse reference file:
ref_panel = SparseReferencePanel("/data/30x/chr20.srp")

To get haplotype calls, the class acts like a sparse matrix:
sparse_matrix = ref_panel[variants, haplotypes]

If full sparse matrix functionality is needed, use all:
sparse_matrix = ref_panel.all

To select a part of the chromosome by position, use range:
sparse_matrix = ref_panel.range(start_pos, end_pos)
"""

import os
import subprocess
from io import BytesIO
from pathlib import Path
from hashlib import blake2b
from typing import List, Tuple, Union
import json
from datetime import datetime
from zipfile import ZipFile, BadZipFile
from tempfile import TemporaryDirectory
import concurrent.futures

from zstd import compress, uncompress
import numpy as np
from scipy import sparse
from cachetools import LRUCache, cachedmethod

from tqdm import tqdm
from joblib import Parallel, delayed

import cyvcf2

from .utils import add_suffix, tqdm_joblib


class SparseReferencePanel:
    """Class for working with ref panels stored as sparse matrix"""

    def __init__(self, filepath: str, cache_size: int = 2) -> None:
        self.filepath = filepath
        if not os.path.exists(self.filepath):
            self._create()
        self.metadata = self._load_metadata()
        self.variant_dtypes = np.dtype(
            [
                tuple(field)
                for field in self.metadata.get(
                    "variant_dtypes",
                    [("chr", "<U21"), ("pos", int), ("ref", "<U21"), ("alt", "<U21")],
                )
            ]
        )
        self.variants: np.ndarray = self._load_variants()
        self.chunks: np.ndarray = self._load_chunks()
        self.ids: List[str] = self._load_ids()
        self.original_ids: List[str] = self._load_original_ids()
        self.sample_ids: List[str] = self._load_sample_ids()
        self._cache = LRUCache(maxsize=cache_size)

    def __len__(self):
        return self.n_variants

    def __repr__(self):
        return (
            f"SparseReferencePanel(filepath={self.filepath}, "
            f"shape=({self.n_variants} variants, {self.n_haps} haplotypes))"
        )

    def __getitem__(self, key: Tuple[Union[int, list, slice]]) -> sparse.csc_matrix:
        """Get sparse matrix of boolean genotypes"""
        if not isinstance(key, tuple):
            raise TypeError("Both variant and haplotype slices must be provided")
        # handle single row
        if isinstance(key[0], int):
            if key[0] > self.n_variants - 1:
                raise IndexError(
                    f"Index {key[0]} out of range for {self.n_variants} variants"
                )
            return self._load_haplotypes(key[0] // self.chunk_size)[
                key[0] % self.chunk_size, key[1]
            ]
        # handle slice
        if isinstance(key[0], slice):
            chunk_step = -1 if key[0].step and key[0].step < 0 else 1
            if not key[0].start and key[0].stop is None:
                return sparse.vstack(
                    [
                        self._load_haplotypes(chunk)[:: key[0].step, key[1]]
                        for chunk in self.chunks[:, 0]
                    ]
                )
            row_stop = (
                min(key[0].stop, self.n_variants)
                if key[0].stop is not None
                else self.n_variants
            )
            chunks = list(
                range(
                    (key[0].start or 0) // self.chunk_size,
                    max(row_stop - 1, 0) // self.chunk_size + 1,
                    chunk_step,
                )
            )
            if len(chunks) == 0:
                raise IndexError("No variants to return")

            chunk_row_stop = row_stop % self.chunk_size or (
                self.chunk_size if row_stop > 0 else row_stop
            )

            if len(chunks) == 1:
                return self._load_haplotypes(chunks[0])[
                    key[0].start % self.chunk_size : chunk_row_stop : key[0].step,
                    key[1],
                ]

            slices = (
                [slice(key[0].start % self.chunk_size, None, key[0].step)]
                + [slice(None, None, key[0].step)] * (len(chunks) - 2)
                + [slice(None, chunk_row_stop, key[0].step)]
            )
            return sparse.vstack(
                [
                    self._load_haplotypes(chunk)[slice_, key[1]]
                    for chunk, slice_ in zip(chunks, slices)
                ]
            )

        # handle list of indexes
        if isinstance(key[0], list) and isinstance(key[0][0], int):
            rows = np.array(key[0])
        elif isinstance(key[0], np.ndarray):
            rows = key[0]
        else:
            raise TypeError("Variant selection must be int, List[int], or slice")

        chunks, splits = np.unique(rows // self.chunk_size, return_index=True)
        if any(chunk not in self.chunks[:, 0] for chunk in chunks):
            raise IndexError(
                f"Index {key[0]} out of range for {self.n_variants} variants"
            )

        chunk_idx = np.split(rows % self.chunk_size, splits[1:])

        return sparse.vstack(
            [
                self._load_haplotypes(chunk).tocsr()[idx, :]
                for chunk, idx in zip(chunks, chunk_idx)
            ]
        ).tocsc()[:, key[1]]

    def _create(self):
        """Create an empty file"""
        print("Creating new sparse matrix archive")
        if os.path.dirname(self.filepath):
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        with ZipFile(self.filepath, mode="x") as archive:
            with archive.open("metadata", "w") as metadata:
                metadata.write(
                    compress(json.dumps({"created_at": str(datetime.now())}).encode())
                )
            with archive.open("variants", "w") as variants:
                variants.write(compress(np.array([]).tobytes()))
            with archive.open("sample_ids", "w") as sample_ids_obj:
                sample_ids_obj.write(compress("\n".join([]).encode()))
            with archive.open("chunks", "w") as chunks:
                chunks.write(compress(np.array([], dtype=int).tobytes()))

    def _save(self, hap_dir: str):
        """Update archive"""
        self.metadata["updated_at"] = str(datetime.now())
        with ZipFile(self.filepath, mode="w") as archive:
            with archive.open("metadata", "w") as metadata:
                metadata.write(compress(json.dumps(self.metadata).encode()))
            with archive.open("variants", "w") as variants:
                variants.write(compress(self.variants.tobytes()))
            with archive.open("IDs", "w") as ids:
                ids.write(compress("\n".join(self.ids).encode()))
            with archive.open("original_IDs", "w") as original_ids:
                original_ids.write(compress("\n".join(self.original_ids).encode()))
            with archive.open("chunks", "w") as chunks:
                chunks.write(compress(self.chunks.tobytes()))
            with archive.open("sample_ids", "w") as sample_ids_file:
                sample_ids_file.write(compress("\n".join(self.sample_ids).encode()))
            for file in Path(hap_dir).iterdir():
                archive.write(file, arcname=os.path.join("haplotypes", file.name))

    def _load_metadata(self) -> dict:
        """Load metadata from archive"""
        with ZipFile(self.filepath, mode="r") as archive:
            with archive.open("metadata") as obj:
                return json.loads(uncompress(obj.read()))

    def _load_variants(self) -> np.ndarray:
        """Load variants from archive"""
        with ZipFile(self.filepath, mode="r") as archive:
            with archive.open("variants") as obj:
                return np.frombuffer(uncompress(obj.read()), dtype=self.variant_dtypes)

    def _load_ids(self) -> List[str]:
        """Load string formatted variant IDs from archive"""
        try:
            with ZipFile(self.filepath, mode="r") as archive:
                with archive.open("IDs") as obj:
                    return uncompress(obj.read()).decode().split("\n")
        except KeyError:
            return [
                "-".join([str(col) for col in variant]) for variant in self.variants
            ]

    def _load_original_ids(self) -> List[str]:
        """Load original vcf/bcf ID field from archive"""
        try:
            with ZipFile(self.filepath, mode="r") as archive:
                with archive.open("original_IDs") as obj:
                    return uncompress(obj.read()).decode().split("\n")
        except KeyError:
            return self._load_ids()

    def _load_sample_ids(self) -> List[str]:
        """Load sample IDs from archive"""
        try:
            with ZipFile(self.filepath, mode="r") as archive:
                if "sample_ids" in archive.namelist():
                    with archive.open("sample_ids") as obj:
                        return uncompress(obj.read()).decode().split("\n")
                else:
                    print("Warning: 'sample_ids' not found in the archive.")
                    return []
        except BadZipFile:
            print("Error: Invalid zip archive format.")
            return []
        except Exception as e:
            print(f"An error occurred: {str(e)}")
            return []

    def _determine_start_position(self, vcf_path: Path) -> int:
        for variant in cyvcf2.VCF(str(vcf_path)):
            return variant.POS

    def _determine_chunk_ranges(
        self, vcf_path: Path, chr_length: int, num_variants: int
    ):
        bp_per_variant = chr_length / num_variants
        bp_per_chunk = bp_per_variant * self.chunk_size
        current_start = self._determine_start_position(vcf_path)
        ranges = []
        while current_start < chr_length:
            end = min(current_start + bp_per_chunk, chr_length)
            ranges.append((int(current_start), int(end)))
            current_start = end + 1
        # Update the last element to 100Gb in order to ensure that no variants
        # are discarded due to assumptions about chromosome length
        ranges[-1] = (ranges[-1][0], 100000000000)
        return ranges

    def _get_vcf_stats(self, vcf_path: Path):
        cmd = ["bcftools", "index", "--stats", vcf_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f"Error executing `{cmd}`: {result.stderr}")
        lines = [line.split("\t") for line in result.stdout.splitlines()]
        if len(lines) > 1:
            raise ValueError("Only one chromosome per file is supported")
        chrom, chr_length, num_variants = lines[0]
        if chr_length == ".":
            chr_length = 100000000000
        return chrom, int(chr_length), int(num_variants)

    def _ingest_variants_from_vcf(self, vcf_path: Path, threads: int = os.cpu_count()):
        chrom, chr_length, num_variants = self._get_vcf_stats(vcf_path)
        self._get_contig_field(chrom, vcf_path)
        chunk_ranges = self._determine_chunk_ranges(vcf_path, chr_length, num_variants)

        def process_chunk(args):
            start, end, chrom, vcf_path = args
            cmd = [
                "bcftools",
                "query",
                "-r",
                f"{chrom}:{start}-{end}",
                "-f",
                "%CHROM\t%POS\t%REF\t%ALT\t%ID\n",
                vcf_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode:
                raise ValueError(f"Error executing `{cmd}`: {result.stderr}")

            return [
                tuple(line.strip().split("\t")) for line in result.stdout.splitlines()
            ]

        # Parallel processing of chunk_ranges
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            chunk_variants_list = list(
                tqdm(
                    executor.map(
                        process_chunk,
                        [(start, end, chrom, vcf_path) for start, end in chunk_ranges],
                    ),
                    total=len(chunk_ranges),
                    desc="Ingest variants from the input",
                    ncols=75,
                    bar_format="{desc}:\t\t\t{percentage:3.0f}% in {elapsed}",
                )
            )

        # Flatten the list of lists
        all_lines = [item for sublist in chunk_variants_list for item in sublist]
        unique_lines = list(dict.fromkeys(all_lines))
        variants = [line[:4] for line in unique_lines]
        self.original_ids = [line[4] for line in unique_lines]

        self.sample_ids = cyvcf2.VCF(str(vcf_path)).samples

        self._ingest_variants(chrom, variants)

    def _ingest_variants_from_pbwt(self, bcf_path: Path):
        """Get the variants and sample IDs from the pbwt files"""
        self.sample_ids = bcf_path.with_suffix(".samples").read_text().splitlines()
        variants = [
            tuple(line.strip().split("\t"))
            for line in bcf_path.with_suffix(".sites").read_text().splitlines()
        ]
        chrom = variants[0][0]
        self._get_contig_field(chrom, bcf_path)
        self._ingest_variants(chrom, variants)

    def _ingest_variants(self, chrom: str, variants: List[Tuple[str]]):
        variant_dtypes = [
            ("chr", f"<U{len(str(chrom))}"),
            ("pos", "int"),
            ("ref", "<U8"),
            ("alt", "<U8"),
        ]
        self.variant_dtypes = np.dtype(variant_dtypes)

        self.variants = np.fromiter(
            [
                (
                    row[0],
                    int(row[1]),
                    blake2b(row[2].encode(), digest_size=8).hexdigest(),
                    blake2b(row[3].encode(), digest_size=8).hexdigest(),
                )
                for row in variants
            ],
            dtype=self.variant_dtypes,
        )
        self.ids = ["-".join([str(col) for col in line]) for line in variants]

        self.chunks = np.array(
            [
                [idx, int(chunk[0][1]), int(chunk[-1][1])]
                for idx, chunk in enumerate(
                    np.split(
                        self.variants,
                        range(self.chunk_size, self.variants.size, self.chunk_size),
                    )
                )
            ],
            dtype=int,
        )

        self.metadata.update(
            {
                "chromosome": str(chrom),
                "n_variants": int(self.variants.size),
                "min_position": int(self.variants[0][1]),
                "max_position": int(self.variants[-1][1]),
                "n_chunks": self.chunks.shape[0],
                "n_samples": len(self.sample_ids),
                "variant_dtypes": variant_dtypes,
            }
        )

    def _ingest_original_ids(self, vcf_path: Path):
        result = subprocess.run(
            ["bcftools", "query", "-f", "%ID\n", vcf_path],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise ValueError(f"Error reading IDs with bcftools: {result.stderr}")

        self.original_ids = result.stdout.splitlines()

    def _get_contig_field(self, chrom: str, vcf_path: Path):
        for line in cyvcf2.VCF(str(vcf_path)).raw_header.splitlines():
            if line.startswith(f"##contig=<ID={chrom},"):
                self.metadata["contig_field"] = line
                break
        else:
            self.metadata["contig_field"] = f"##contig=<ID={chrom}>"

    def _load_chunks(self) -> np.ndarray:
        with ZipFile(self.filepath, mode="r") as archive:
            with archive.open("chunks") as obj:
                chunks_ = np.frombuffer(uncompress(obj.read()), dtype=int)
        if chunks_.size:
            return np.reshape(chunks_, (self.n_chunks, 3))
        return chunks_

    @cachedmethod(lambda self: self._cache)
    def _load_haplotypes(self, chunk: int) -> sparse.csc_matrix:
        """Load a sparse matrix from archived npz"""
        with ZipFile(self.filepath, mode="r") as archive:
            with archive.open(f"haplotypes/{chunk}.npz") as obj:
                return sparse.load_npz(BytesIO(uncompress(obj.read())))

    def _load_dosage(self, chunk: int) -> sparse.csc_matrix:
        """Load a sparse matrix dosage from archived npz"""
        loaded_chunk = self._load_haplotypes(chunk)
        return loaded_chunk[:, ::2] + loaded_chunk[:, 1::2]

    def _calculate_maf(self, chunk: int) -> np.ndarray:
        """Calculate minor allele frequency (maf) for a given chunk."""
        loaded_chunk = self._load_haplotypes(chunk)
        freqs = (loaded_chunk.sum(axis=1) / self.metadata["n_haps"]).A.squeeze()
        mask = freqs > 0.5
        freqs[mask] = 1 - freqs[mask]
        return freqs

    def _std_out_to_sparse(self, command: str, chunk: int, tmpdir: str) -> tuple:
        """Convert std_out of command to sparse matrix"""
        result = subprocess.run(command, shell=True, capture_output=True)
        if result.returncode:
            raise ValueError(f"Error executing `{command}`: {result.stderr}")
        if result.stdout == b"":
            raise ValueError(f"No genotypes returned by `{command}`")

        # account for when the same position appears in multiple chunks
        offset = 0
        if self.chunks[chunk][1] == self.chunks[chunk - 1][2]:
            for variant in range(chunk * self.chunk_size - 1, 0, -1):
                if self.variants[variant][1] == self.chunks[chunk][1]:
                    offset += 1
                else:
                    break
        matrix = sparse.csc_matrix(
            np.loadtxt(BytesIO(result.stdout), delimiter="|", dtype=np.bool_)[
                offset : offset + self.chunk_size, :
            ]
        )
        # confirm correct number of variants
        assert matrix.shape[0] == self.chunk_size or (
            chunk == self.n_chunks - 1
            and matrix.shape[0] == self.n_variants % self.chunk_size
        )
        npz_ = BytesIO()
        sparse.save_npz(npz_, matrix)
        npz_.seek(0)
        with open(os.path.join(tmpdir, f"{chunk}.npz"), "wb") as fout:
            fout.write(compress(npz_.read()))
        return matrix.shape[1]

    def _ingest_haplotypes(self, commands: List[str], threads: int = 1):
        """Load haplotypes from non-npz file"""
        with TemporaryDirectory() as tmpdir:
            hap_dir = os.path.join(tmpdir, "haplotypes")
            os.makedirs(hap_dir)
            with tqdm_joblib(
                total=len(commands),
                desc="Saving haplotypes as sparse matrices",
                ncols=75,
                bar_format="{desc}:\t\t{percentage:3.0f}% in {elapsed}",
            ):
                haps = Parallel(
                    n_jobs=threads,
                )(
                    delayed(self._std_out_to_sparse)(command, chunk, hap_dir)
                    for chunk, command in enumerate(commands)
                )
            hap_counts = list(set(haps))
            assert len(hap_counts) == 1

            self.metadata.update({"n_haps": int(hap_counts[0])})
            self._save(hap_dir)

    def _ingest_xsi_haplotypes(self, xsi_path: str, threads: int = 1):
        """Load haplotypes from xsi file"""
        # -r includes overlapping indels, but is faster than -t, so use both
        commands = [
            (
                f"xsqueezeit -x -f {xsi_path} -p "
                f'-r "{self.chromosome}:{chunk[1]}-{chunk[2]}" | '
                f"bcftools query -t {self.chromosome}:{chunk[1]}-{chunk[2]} "
                "-f '[|%GT]\n' | sed s'/|//'"
            )
            for chunk in self.chunks
        ]
        self._ingest_haplotypes(commands, threads)

    def from_xsi(
        self,
        xsi_path: Union[str, Path],
        chunk_size: int = 10**4,
        threads: int = 1,
        replace_file: bool = False,
    ):
        """Convert an xsi file to sparse matrix"""
        xsi_path = Path(xsi_path).resolve()
        if self.n_variants > 0 and not replace_file:
            print("Variants have already been loaded")
            return self
        if not xsi_path.exists():
            raise FileNotFoundError(f"Missing input file: {xsi_path}")
        xsi_bcf = add_suffix(xsi_path, "_var.bcf")
        if not xsi_bcf.exists():
            raise FileNotFoundError(f"Missing input file: {xsi_bcf}")
        self.metadata["source_file"] = str(xsi_path)
        self.metadata["chunk_size"] = chunk_size
        if (
            xsi_path.with_suffix(".sites").exists()
            and xsi_path.with_suffix(".samples").exists()
        ):
            self._ingest_variants_from_pbwt(xsi_path)
            self._ingest_original_ids(xsi_bcf)
        else:
            self._ingest_variants_from_vcf(xsi_bcf, threads)
        self._ingest_xsi_haplotypes(xsi_path, threads)

        return self

    def _ingest_bcf_haplotypes(self, bcf_path: str, threads: int = 1):
        """Load haplotypes from vcf/bcf file"""
        # -r includes overlapping indels, but is faster than -t, so use both
        commands = [
            (
                f"bcftools view -r {self.chromosome}:{chunk[1]}-{chunk[2]} "
                f"{bcf_path} | bcftools query -f '[|%GT]\n' "
                f"-t {self.chromosome}:{chunk[1]}-{chunk[2]} | sed s'/|//'"
            )
            for chunk in self.chunks
        ]
        self._ingest_haplotypes(commands, threads)

    def from_bcf(
        self,
        bcf_path: Union[str, Path],
        chunk_size: int = 10**4,
        threads: int = 1,
        replace_file: bool = False,
    ):
        """Convert a vcf/bcf file to sparse matrix"""
        bcf_path = Path(bcf_path).resolve()
        if self.n_variants > 0 and not replace_file:
            print("Variants have already been loaded")
            return self
        if not bcf_path.exists():
            raise FileNotFoundError(f"Missing input file: {bcf_path}")
        if not (
            add_suffix(bcf_path, ".tbi").exists()
            or add_suffix(bcf_path, ".csi").exists()
        ):
            print(f"Indexing input file: {bcf_path}")
            subprocess.run(
                ["bcftools", "index", bcf_path, "--threads", threads], check=True
            )

        self.metadata["source_file"] = str(bcf_path)
        self.metadata["chunk_size"] = chunk_size
        if (
            bcf_path.with_suffix(".sites").exists()
            and bcf_path.with_suffix(".samples").exists()
            and ".vcf" not in bcf_path.suffixes
        ):
            self._ingest_variants_from_pbwt(bcf_path)
            self._ingest_original_ids(bcf_path)
        else:
            self._ingest_variants_from_vcf(bcf_path, threads)
        self._ingest_bcf_haplotypes(bcf_path, threads)

        return self

    @property
    def n_variants(self) -> int:
        """Get number of variants"""
        return self.metadata.get("n_variants", 0)

    @property
    def n_haps(self) -> int:
        """Get number of haplotypes"""
        return self.metadata.get("n_haps", 0)

    @property
    def n_samples(self) -> int:
        """Get number of samples"""
        return len(self.sample_ids)

    @property
    def shape(self) -> int:
        """Get shape of full matrix"""
        return (self.n_variants, self.n_haps)

    @property
    def n_chunks(self) -> int:
        """Get number of chunks"""
        return self.metadata.get("n_chunks", 0)

    @property
    def chunk_size(self) -> int:
        """Number of variants per chunk"""
        return self.metadata.get("chunk_size", 0)

    @property
    def max_position(self) -> int:
        """Assumes only 1 chromosome and sorted variants"""
        return self.metadata.get("max_position", 0)

    @property
    def chromosome(self) -> str:
        """Get chromosome"""
        return self.metadata.get("chromosome", "")

    @property
    def contig_field(self) -> str:
        """Get contig field"""
        return self.metadata.get("contig_field", "")

    @property
    def empty(self) -> bool:
        return self.n_variants == 0 or self.n_haps == 0

    @property
    def all(self) -> sparse.csc_matrix:
        """Get unsliced sparse matrix of all boolean genotypes"""
        return sparse.vstack(
            [self._load_haplotypes(chunk) for chunk in self.chunks[:, 0]]
        )

    def range(
        self, min_bp: int, max_bp: int, inclusive: bool = True
    ) -> sparse.csc_matrix:
        """Get sparse matrix of boolean genotypes in position range"""
        max_bp += int(inclusive)
        positions = np.array([variant[1] for variant in self.variants], dtype=int)
        return self[positions.searchsorted(min_bp) : positions.searchsorted(max_bp), :]
