import argparse
import subprocess
import sys
from Bio import SeqIO, motifs
from Bio.Alphabet import IUPAC
import re
from collections import Counter
import pysam
from tqdm import *

# Authored by: Tyler Shimko

def call_peaks(control_bam, treatment_bam, macs_args, output, reference):
    """
    Provide an interface to MACS2 to call peaks from ChIP-seq data

    Args:
        control_bam (str) - file path to the control bam file
        treatment_bam (str) - file path to the treatment bam file
        macs_args (list) - command line arguments to pass to MACS2
        output (str) - prefix for output files
        reference (str) - path to the reference genome fasta file
    """

    # Construct the call
    args = ["macs2", "callpeak", "--nomodel", "-g", get_genome_size(reference),
            "-n", output, "-t"]
    args.extend(treatment_bam)

    if control_bam != "":
        args.extend(["-c", control_bam])

    args.extend(macs_args)

    # Call MACS2
    subprocess.call(args)


def calc_coverage(bam):
    """
    Calculate coverage at each genomic position from a bam file

    Args:
        bam (str) - file path to bam from which to calculate coverage

    Return:
        Genome coverage as a dictioary of Counter objects, one for each
        chromosome.
    """

    # Read the bam file
    bam = pysam.AlignmentFile(bam, "rb")

    # Initialize a coverage dictionary
    coverage = {}

    # For each line, with a nice progress meter
    for line in tqdm(bam):

        # The pysam bam parser doesn't like unmapped reads, so skip these
        try:
            # If a key in the coverage dictioary does not exist for the current
            # chromosome, add one with a Counter for the positions
            if line.reference_name not in coverage.keys():
                coverage[line.reference_name] = Counter()

            # Update the appropriate entry in the appropriate counter each
            # time a read covers that position
            for pos in range(line.reference_start,
                             line.reference_start + len(line.query_sequence)):
                coverage[line.reference_name][pos] += 1
        except:
            pass
    return coverage


def get_genome_size(reference):
    """
    Get the effective genome size of a reference genome. Per MACS2
    documentation, this is .7-.9 * genome size. For simplicity, .9 is chosen
    here.

    Args:
        reference (str) - path to referece genome

    Return:
        The estimated effective mapping size of the genome.
    """

    with open(reference, "r") as ref:
        records = SeqIO.parse(ref, "fasta", alphabet=IUPAC.unambiguous_dna)
        return str(round(sum([len(record.seq) for record in records]) * .9))


def find_pwm_hits(narrow_peak, reference, pfm, output, treat_cov):
    """
    Search each peak for the best match against the specified position
    frequency matrix

    Args:
        narrow_peak (str) - path to the narrowPeak file output by MACS2
        reference (str) - file path to the reference genome
        pfm (str) - file path to the position frequency matrix
        output (str) - prefix for the output file
    """

    # Open the peaks and reference genome files
    with open(narrow_peak, "r") as peaks, open(reference, "r") as ref:
        # Parse the reference genome into a dictionary
        records = SeqIO.parse(ref, "fasta", alphabet=IUPAC.unambiguous_dna)
        ref_seq = {record.id: record for record in records}

        # Open and parse the position frequency matrix
        with open(pfm, "r") as pfm:
            matrix = motifs.parse(pfm, "jaspar")[0]
            pwm = matrix.counts.normalize(pseudocounts=.5)
            pssm = pwm.log_odds()

        # Open the output file
        with open(output + "_centeredpeaks.bed", "w") as out_bed, \
                open(output + "_centeredpeaks.fasta", "w") as out_fasta:

            # Write a line for each centered peak in the output file
            for peak in peaks:
                split_peak = peak.strip().split("\t")
                peak_chrom = split_peak[0]
                peak_start = int(split_peak[1])
                peak_end = int(split_peak[2])
                seq = ref_seq[peak_chrom].seq[peak_start:peak_end]

                hits = [(pos, score) for pos, score in pssm.search(seq)]

                hits.sort(key=lambda hit: hit[1], reverse=True)

                recenter_peak(out_bed, out_fasta, ref_seq, peak_chrom,
                              peak_start, peak_end, 100, hits, matrix,
                              treat_cov)


def recenter_peak(out_bed_handle, out_fasta_handle, ref_seq, chrom, seq_start,
                  seq_end, slop, hits, matrix, treat_cov):
    """
    Recenter peaks on the best hit against the position frequency matrix

    Args:
        out_bed_handle (handle) - handle for the output bed file
        out_fasta_handle (handle) - handle for the output fasta file
        ref_seq (dict) - reference genome sequence stored as a dictionary
        chrom (str) - chromosome identifier
        seq_start (int) - start position of the putative binding sequence
        seq_end (int) - end position of the putative binding sequence
        slop (int) - amount to extend out from the center of the peak on either
            side
        hits (list) - list of hits against pfm in the sequence
        matrix (pfm matrix) - parsed position frequency matrix
    """

    # If hits against the pfm are found
    if hits:

        # Parse the hit for position information
        hit_pos = hits[0][0]
        hit_start = seq_start + hit_pos
        hit_end = seq_start + hit_pos + len(matrix)
        start_adjusted = hit_start - slop
        end_adjusted = hit_end + slop

        # Get the sequence under the peak
        seq = ref_seq[chrom].seq[start_adjusted:end_adjusted]
        rev_seq = seq.reverse_complement()

        # Get the mean coverage across the peak
        coverage = [treat_cov[chrom][pos]for pos in
                    range(hit_start, hit_end)]
        mean_cov = sum(coverage) / (hit_end - hit_start)

        # Determine if hit against psm was on forward or reverse strand
        if hit_pos < 0:
            strand = "-"
            seq = rev_seq
        else:
            strand = "+"

        # Look for the consensus sequence under the peak
        cons_forward = re.search(str(matrix.consensus), str(seq))
        cons_reverse = re.search(str(matrix.consensus), str(rev_seq))
        contains_cons = cons_forward or cons_reverse

        if contains_cons:
            color = "255,0,0"
        else:
            color = "0,0,255"

        # Construct and write the line corresponding to the peak to the output
        # BED format file
        line = [chrom, start_adjusted, end_adjusted, seq, mean_cov, strand, 0,
                0, color]

        line = [str(element) for element in line]
        out_bed_handle.write("\t".join(line) + "\n")

        # Write the centered peak sequence to a FASTA file to be used by
        # DNAShapeR to examine DNA shape
        out_fasta_handle.write(">{}|{}\n{}\n".format(chrom, start_adjusted,
                                                     seq))


def main(argv):
    """
    Find and recenter peaks around the best hit for a given position weight
    matrix

    Args:
        argv (list) - list of command line arguments
    """

    # Construct an argument parser
    parser = argparse.ArgumentParser(description="Call ChIP-seq peaks using \
                                     MACS2 and recenter the peaks on the best \
                                     hit from a position weight matrix")
    parser.add_argument("--control", help="control sequencing run, can be \
                        input, IgG pulldown, etc", type=str, nargs="?",
                        metavar="control", default="")
    parser.add_argument("--reference", help="file path to the reference \
                        genome", type=str, nargs=1, metavar="reference")
    parser.add_argument("--extend", help="distance to extend sequence space \
                        on either side of the best hit against the provided \
                        position weight matrix", type=int, default=100,
                        nargs=1, metavar="distance")
    parser.add_argument("--pfm", help="position frequency matrix from JASPAR \
                        for the protein of interest", nargs=1, metavar="pfm")
    parser.add_argument("input", help="treatment files (ChIP-ed sample) in \
                        bam format", nargs="*", type=str, metavar="bam")
    parser.add_argument("output", help="output file prefix", nargs=1,
                        type=str, metavar="prefix")

    # Parse command line arguments
    args, macs_args = parser.parse_known_args(argv[1:])

    output = args.output[0]
    reference = args.reference[0]
    pfm = args.pfm[0]

    # Call peaks
    print("Calling ChIP-seq peaks...")
    call_peaks(args.control, args.input, macs_args, output, reference)

    # Get coverage for each position
    print("Calculating coverage for treatment sample...")
    treat_cov = calc_coverage(args.input[0])

    # Find pfm hits, recenter peaks and write to file
    print("Recentering ChIP-seq peaks...")
    find_pwm_hits(output + "_peaks.narrowPeak", reference, pfm, output,
                  treat_cov)

# Read command line arguments if the script is called directly
if __name__ == "__main__":
    main(sys.argv)
