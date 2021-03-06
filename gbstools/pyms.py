"""
A utility for parsing output from Hudson's MS and using it to simulate
a GBS SNP data, including the frequencies of 'A', 'a', and '-' alleles 
as described in the docs/gbstools_notes.pdf.
"""

import random

class Reader():
    """Parser class for MS output files. Generates ``Sample`` objects."""
    def __init__(self, msfile, fraglen=500, sitelen=6, siteprob=0.0011, readlen=101, variantsonly=False):
        self.msfile = msfile
        self.samples = None
        self.seeds = None
        self.command = None
        self.parse_header()
        # Length of GBS fragment.
        self.fraglen = fraglen
        # Length of restriction site.
        self.sitelen = sitelen
        self.siteprob = siteprob
        self.readlen = readlen
        # Flag=true when each site has a segregating restriction site variant.
        self.variantsonly = variantsonly
        # Set the seed for the random number generator.
        self.rand = random.Random(0)
        self.sitenum = 0
    
    def parse_header(self):
        '''Parse the header line of the MS output file.'''
        header = []
        line = self.msfile.readline()
        line = line.strip()
        fields = line.split()
        self.samples = int(fields[1])
        self.command = line
        line = self.msfile.readline()
        self.seeds = line.strip()
        while line != '//':
            line = self.msfile.readline()
            line = line.strip()
        return(None)

    def __iter__(self):
        return(self)
    
    def next(self):
        '''Generate a "Sample" object that contains information from one MS site.'''
        self.sitenum += 1
        segsites = None
        positions = []
        haplotypes = []
        for line in self.msfile:
            line = line.strip()
            fields = line.split()
            if line == '//':
                break
            elif not line:
                continue
            elif fields[0] == 'segsites:':
                # Record the number of segregating sites.
                segsites = int(fields[1])
            elif fields[0] == 'positions:':
                # Convert the MS position field to integers in sequence 0-N.
                positions = [int(self.fraglen * float(pos)) for pos in fields[1:]]
            else:
                # Record the individual haplotypes.
                haplotypes.append([int(i) for i in list(fields[0])])
        if segsites is not None:
            # Create a "Sample" object.
            return(Sample(segsites, positions, haplotypes, self.fraglen, self.sitelen, self.siteprob,
                          self.readlen, self.variantsonly, self.rand, self.sitenum))
        raise StopIteration

class Sample():
    """Class for storing and manipulating data from one MS coalescent sample."""
    def __init__(self, segsites, positions, haplotypes, fraglen, sitelen, siteprob,
                 readlen, variantsonly, rand, sitenum):
        self.segsites = segsites
        self.positions = positions
        self.haplotypes = haplotypes
        self.fraglen = fraglen    # Number of samples.
        self.sitelen = sitelen    # Length of restriction site.
        self.siteprob = siteprob    # Per-base probability of seeing a restriction site.
        self.readlen = readlen
        self.variantsonly = variantsonly
        self.rand = rand
        self.sitenum = sitenum
        self.minus_haplotype = self.get_minus_haplotype()
        (self.dcount, self.missing, self.ac, self.ac_sampled, self.hets, 
         self.hets_sampled, self.background, self.genotypes, self.true_genotypes,
         self.hets_missing) = self.counts()
        
    def get_minus_haplotype(self):
        '''Get list of SNP alleles that cause the '-' haplotype (0, 1, or None).'''
        minus_haplotype = []
        if self.variantsonly:    # Should each locus with segregating sites have an rs variant?
            if self.positions:
                minus_haplotype = [None] * len(self.positions)
                index = self.rand.choice(range(len(minus_haplotype)))    # Index in haplotype.
                minus_haplotype[index] = self.rand.choice((0,1))    # Equally likely that derived allele is '-' or '+'.
                self.positions[index] = -1    # Reset the position of the rs variant so it doesn't get 'sequenced'.
        else:
            for pos in self.positions:
                # Assume positions in the first L and last L bp of the fragment are restriction sites.
                if pos <= self.sitelen or pos >= self.fraglen - self.sitelen:
                    # For the restriction site it's equally likely that the derived allele is '-' or '+'.
                    minus_haplotype.append(self.rand.choice((0,1)))
                else:
                    # SNPs in interior of N bp fragment can also result in '-' haplotype.
                    # Approximate chance of seeing a restriction site at any given base (for non-palindromic sites).
                    if self.rand.random() < self.siteprob:
                        minus_haplotype.append(self.rand.choice((0,1)))
                    else:
                        minus_haplotype.append(None)
        return(minus_haplotype)

    def counts(self):
        '''Calculate DCount, Missing, AC, ACgbs, `-` background, GT list.'''
        n = len(self.haplotypes)
        # Indices of alleles in the haplotype that are in the 2 x 101 bp region.
        indices = []
        for i in range(self.segsites):
            # Forward read indices.
            if (self.positions[i] > self.sitelen and 
                self.positions[i] <= self.sitelen + self.readlen):
                indices.append(i)
            # Reverse read indices.
            elif (self.positions[i] >= self.fraglen - (self.sitelen + self.readlen) and 
                  self.positions < self.fraglen - self.sitelen):
                indices.append(i)

        # Get haplotypes.
        haplotypes = []
        sequenced_haplotypes = []
        dropout_haplotypes = []
        dcount = 0
        for hap in self.haplotypes:
            haplotypes.append([hap[i] for i in indices])
            if sum([a == b for a, b in zip(hap, self.minus_haplotype)]) == 0:
                sequenced_haplotypes.append([hap[i] for i in indices])
            else:
                sequenced_haplotypes.append([None] * len(indices))
                dropout_haplotypes.append([hap[i] for i in indices])
                dcount += 1

        # Get true allele counts.
        ac = [sum(i) for i in zip(*haplotypes)]
                    
        # Initialize lists of sequenced genotypes, sampled AC, and sampled hets.
        genotypes = [[] for i in range(len(ac))]
        ac_sampled = [0] * len(ac)
        missing = [0] * len(ac)

        # Iterate through pairs of samples (a, b).
        hap_pairs = zip(sequenced_haplotypes[0::2], sequenced_haplotypes[1::2])
        for hap_pair in hap_pairs:
            loci = zip(*hap_pair)
            for i in range(len(loci)):    # Iterate over loci and get genotypes.
                if loci[i] == (0, 0):
                    genotypes[i].append('0/0')
                elif loci[i] == (0, 1) or loci[i] == (1, 0):
                    genotypes[i].append('0/1')
                    ac_sampled[i] += 1
                elif loci[i] == (1, 1):
                    genotypes[i].append('1/1')
                    ac_sampled[i] += 2
                elif loci[i] == (0, None) or loci[i] == (None, 0):
                    genotypes[i].append('0/.')
                elif loci[i] == (1, None) or loci[i] == (None, 1):
                    genotypes[i].append('1/.')
                    ac_sampled[i] += 2
                else:
                    genotypes[i].append('./.')
                    missing[i] += 1
        # Calculate number of observed hets.
        hets_sampled = [geno.count('0/1') for geno in genotypes]

        # Determine true genotypes and calculate true number of hets.
        hets = [0] * len(ac)
        true_hap_pairs = zip(haplotypes[0::2], haplotypes[1::2])
        true_genotypes = [[] for i in range(len(ac))]
        for hap_pair in true_hap_pairs:
            loci = zip(*hap_pair)
            for i in range(len(loci)):    # Iterate over loci and get genotypes.
                if loci[i] == (0, 0):
                    true_genotypes[i].append('0/0')
                elif loci[i] == (0, 1) or loci[i] == (1, 0):
                    hets[i] += 1
                    true_genotypes[i].append('0/1')
                elif loci[i] == (1, 1):
                    true_genotypes[i].append('1/1')

        # Calculate number of hets with './.' observed (missing) genotype.
        hets_missing = [0] * len(ac)
        for i in range(len(true_genotypes)):
            for j in range(len(true_genotypes[i])):
                if true_genotypes[i][j] == '0/1' and genotypes[i][j] == './.':
                    hets_missing[i] += 1

        # Determine the allelic background for the dropout allele.
        dropout_alleles = zip(*dropout_haplotypes)    # Make list of tuples of alleles at each locus.
        if dropout_alleles:
            background = []
            for alleles in dropout_alleles:
                if 0 in alleles:
                    if 1 in alleles:
                        background.append('both')    # '-' is seen with both derived and ancestral alleles.
                    else:
                        background.append('ancestral')    # '-' is only seen with the ancestral allele.
                else:
                    background.append('derived')    # '-' is only seen with the derived allele.
        else:
            background = [None] * len(indices)

        return((dcount, missing, ac, ac_sampled, hets, hets_sampled, background, genotypes, true_genotypes, hets_missing))
