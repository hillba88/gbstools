import em
import vcf
import pysam
from numpy import median
import math
from vcf.model import make_calldata_tuple
from collections import namedtuple
import warnings

try:
    from collections import Counter
except ImportError:
    from counter import Counter

try:
    from collections import OrderedDict
except:
    from ordereddict import OrderedDict

""" Confusion matrix for Illumina sequencers. Keyed by [actual base][read base]
(see DePristo et al 2010. """
CONFUSION_MATRIX = {'A':{'A':None, 'C':0.577, 'G':0.171, 'T':0.252},
                    'C':{'A':0.349, 'C':None, 'G':0.113, 'T':0.539},
                    'G':{'A':0.319, 'C':0.051, 'G':None, 'T':0.630},
                    'T':{'A':0.458, 'C':0.221, 'G':0.320, 'T':None}}

"""INFO fields to be added to the vcf header by GBStools."""
_Info = namedtuple('Info', ['id', 'num', 'type', 'desc'])
INFO = (_Info('DLR', None, 'Float', 'Dropout likelihood ratio (GBStools)'),
        _Info('DFreq', None, 'Float', 'Dropout allele frequency estimated by EM (GBStools)'),
        _Info('AFH1', None, 'Float', 'Allele frequency estimated by EM (GBStools)'),
        _Info('AFH0', None, 'Float', 'Null hypothesis allele frequency estimated by EM (GBStools)'),
        _Info('LambdaH1', None, 'Float', 'Normalized mean coverage estimated by EM (GBStools)'),
        _Info('LambdaH0', None, 'Float', 'Null hypothesis normalized mean coverage estimated by EM (GBStools)'),
        _Info('DigestH1', None, 'Float', 'Digest failure rate estimated by EM (GBStools)'),
        _Info('DigestH0', None, 'Float', 'Null hypothesis digest failure rate estimated by EM (GBStools)'),
        _Info('IterationH1', None, 'Integer', 'Number of null hypothesis EM iterations (GBStools)'),
        _Info('IterationH0', None, 'Integer', 'Number of alt hypothesis EM iterations (GBStools)'),
        _Info('EMFailH1', 0, 'Flag', 'EM failure flag'),
        _Info('EMFailH0', 0, 'Flag', 'Null hypothesis EM failure flag'),
        _Info('SelfRS', None, 'String', 'Recognition sites for reads mapped to the SNP'),
        _Info('MateRS', None, 'String', 'Recognition sites for mate pairs of reads mapped to the SNP'),
        _Info('InsMed', None, 'Float', 'Insert size median'),
        _Info('InsMAD', None, 'Float', 'Insert size MAD'))

"""INFO fields to be added to vcf header when --ped option is used."""
PEDINFO = (_Info('DLR', None, 'Float', 'Dropout likelihood ratio (GBStools)'),
           _Info('ParentsH1', None, 'String', 'Maximum-likelihood parental genotypes for alt hypothesis'),
           _Info('ParentsH0', None, 'String', 'Maximum-likelihood parental genotypes for null hypothesis'),
           _Info('LambdaH1', None, 'Float', 'Normalized mean coverage estimated by EM for MLE parental genotypes (GBStools)'),
           _Info('LambdaH0', None, 'Float', 'Null hypothesis normalized mean coverage estimated by EM for MLE parental genotypes (GBStools)'),
           _Info('DigestH1', None, 'Float', 'Digest failure rate estimated by EM for MLE parental genotypes (GBStools)'),
           _Info('DigestH0', None, 'Float', 'Null hypothesis digest failure rate estimated by EM for MLE parental genotypes (GBStools)'),
           _Info('IterationH1', None, 'Integer', 'Number of null hypothesis EM iterations for MLE parental genotypes (GBStools)'),
           _Info('IterationH0', None, 'Integer', 'Number of alt hypothesis EM iterations for MLE parental genotypes (GBStools)'),
           _Info('EMFailH1', 0, 'Flag', 'EM failure flag'),
           _Info('EMFailH0', 0, 'Flag', 'Null hypothesis EM failure flag'),
           _Info('SelfRS', None, 'String', 'Recognition sites for reads mapped to the SNP'),
           _Info('MateRS', None, 'String', 'Recognition sites for mate pairs of reads mapped to the SNP'),
           _Info('InsMed', None, 'Float', 'Insert size median'),
           _Info('InsMAD', None, 'Float', 'Insert size MAD'))

"""FORMAT fields to be added to the vcf header by GBStools."""
_Format = namedtuple('Format', ['id', 'num', 'type', 'desc'])
FORMAT = (_Format('DC', None, 'Integer', 'Dropout allele count'),
          _Format('INS', None, 'Integer', 'Median insert size'),
          _Format('NF', None, 'Integer', 'Normalization factor for sample DP'))

GT_FORMATTED = {(2,0,0):'0/0',
                (1,0,1):'0/.',
                (1,1,0):'0/1',
                (0,2,0):'1/1',
                (0,1,1):'1/.',
                (0,0,2):'./.'}

class Reader():
    """Reader for a VCF file, an iterator returning ``_Marker`` objects."""
    def __init__(self, filename=None, bamlist=None, norm=None, disp_intercept=2.5, 
                 disp_slope=0.0, ped=None, samples=None, dpmode=False):
                 
        """Create a new Reader for a VCF file containing GBS data.

           To get marker data from indexed bam files, use bamlist=mybamlist 
           where mybamlist is in the format of name/file per line.

           To normalize the read coverages according to the relative
           numbers of reads across samples, use norm=<mynormfile>,
           generated by normfactors.py

           To set the coverage dispersion index use the disp option
        """
        # Make a generator for vcf records.
        self._reader = vcf.Reader(filename=filename)
        self.reader = (record for record in self._reader)
        self.filename = filename
        # Make a list of samples to go into the analysis.
        try:
            self.samples = self.parse_samples(samples)
        except:
            self.samples = self._reader.samples
        # Make a list of Samfile objects for fetching read info.
        try:
            self.alignments = self.parse_bamlist(bamlist)
        except:
            self.alignments = None
        # If normfactors file exists, create a hash of its data.
        try:
            self.normfactors = self.parse_norm(norm)
        except:
            self.normfactors = None

        self.disp = {'slope':disp_slope, 'intercept':disp_intercept}
        # Should DP-only mode be used?
        self.dpmode = dpmode
        # Get family info from PED file if it exists.
        try:
            self.family = self.parse_ped(ped)
        except:
            self.family = None

    def parse_samples(self, samples_file):
        '''Parse sample list.'''
        samples = []
        samples_file = open(samples_file, 'r')
        for line in samples_file:
            line = line.strip()
            if line:
                samples.append(line)
        return(samples)

    def parse_bamlist(self, bamlist):
        '''Parse the bamlist file and return a list of Samfile objects.'''
        bamlist = open(bamlist, 'r')
        alignments = {}
        for line in bamlist:
            line = line.strip()
            sample, bam = line.split()
            alignments[sample] = _Samfile(bam, sample)
        if set(alignments.keys()) != set(self.samples):
            message = ("Numbers of samples in Reader.alignments and " 
                       "Reader.samples do not agree. GBStools will attempt to "
                       "use data from the input VCF for the missing samples.")
            warnings.warn(message, Warning)
        return(alignments)

    def parse_norm(self, norm):
        '''Parse the normalization factors file and store data in a hash.'''
        norm = open(norm, 'r')
        # The first two header fields define the bin limits; the rest are sample names.
        header = norm.readline()
        header = header.strip()
        samples = header.split()[1:]
        normfactors = {}    # Hash of keyed by (sample, insert).
        for line in norm:
            line = line.strip()
            fields = line.split()
            insert = fields[0]
            # In RAD-seq the insert size is random, so ''NA'' is used.
            if insert == "NA":
                insert = None
            # In GBS the expected insert size is known for any given site.
            else:
                insert = int(insert)
            nf_row = [float(i) for i in fields[1:]]
            for (sample, nf) in zip(samples, nf_row):
                normfactors[(sample, insert)] = nf
        if set(samples) != set(self.samples):
            message = ("Numbers of samples in Reader.normfactors and "
                       "Reader.samples do not agree. This may cause DP "
                       "normalization errors. GBStools will use the "
                       "default normalization factor (1.0)")
            warnings.warn(message, Warning)
            normfactors = None
        return(normfactors)
    
    def parse_ped(self, ped):
        '''Parse the PED file and return a named tuple of family members.'''
        Family = namedtuple('Family', 'father, mother, children')
        ped = open(ped, 'r')
        offspring = {}
        for line in ped:
            line = line.strip()
            fam_id, indiv_id, father, mother, sex, pheno = line.split()
            # Only consider children that are in the samples list.
            if indiv_id in self.samples:
                try:
                    offspring[(father, mother)].append(indiv_id)
                except:
                    offspring[(father, mother)] = [indiv_id]
        # Get the family with the largest number of offspring in the PED.
        (father, mother) = max(offspring, key=lambda x: len(x))
        children = offspring[(father, mother)]
        return(Family(father, mother, children))

    def __iter__(self):
        return self
   
    def next(self):
        # Extract SNP info from the vcf file.
        vcf_record = self.reader.next()
        chrom = vcf_record.CHROM
        pos = vcf_record.POS - 1
        ref = vcf_record.REF
        alt = vcf_record.ALT
        # Generate a list of ''CallData'' objects in the same order as in vcf.
        calls = []
        for sample in self.samples:
            try:
                # Get read data directly from bam file.
                alignment = self.alignments[sample]
                call = alignment.pileup(chrom, pos, ref, alt[0])
            except:
                try:
                    # Make a dict of PyVCF ``_Call`` objects keyed by sample name.
                    vcf_calls = dict(zip(self._reader.samples, vcf_record.samples))
                    # Get read data from VCF.
                    keys = vcf_calls[sample].data._fields
                    vals = list(iter(vcf_calls[sample].data))
                    data = dict(zip(keys, vals))
                    call = CallData(sample, **data)
                except:
                    message = ("Sample ''%s'' not found in user-supplied VCF "
                               "or in user-supplied list of bam files." % sample)
                    raise Exception(message)
            # Look up the normalization factor based on insert size.            
            try:
                call.NF = self.normfactors[(sample, int(call.INS))]
            except:
                call.NF = 1.0
            calls.append(call)
                
        # If DP-only mode is being used, set PL to None.
        if self.dpmode:
            for call in calls:
                call.PL = None
        # If PED-mode is being used, set family flags in calls.
        if self.family:
            for call in calls:
                if call.sample == self.family.father:
                    call.is_father = True
                elif call.sample == self.family.mother:
                    call.is_mother = True
                elif call.sample in self.family.children:
                    call.is_child = True
        # Create a dictionary of vcf INFO field tags.
        info = OrderedDict()
        # Calculate insert size median and MAD.
        inserts = [ins for sample in calls for ins in sample.inserts]
        if inserts:
            ins_med = median(inserts)
            ins_mad = median([abs(ins - ins_med) for ins in inserts])
        else:
            ins_med = None
            ins_mad = None
        info['InsMed'] = ins_med
        info['InsMAD'] = ins_mad
        # Get self and mate enzymes.
        self_rs_list = [rs for sample in calls for rs in sample.self_rs]
        mate_rs_list = [rs for sample in calls for rs in sample.mate_rs]
        try:
            self_rs_tally = Counter(self_rs_list).most_common(1)
            self_rs, count = self_rs_tally.pop()
            self_rs = self_rs.replace(';', ',')
            info['SelfRS'] = "%s,%i" % (self_rs, count)
        except:
            pass
        try:
            mate_rs_tally = Counter(mate_rs_list).most_common(1)
            mate_rs, count = mate_rs_tally.pop()
            mate_rs = mate_rs.replace(';', ',')
            info['MateRS'] = "%s,%i" % (mate_rs, count)
        except:
            pass

        # Generate ''Marker'' or ''PedMarker'' object.
        if not self.family:
            marker = Marker(rec=vcf_record, calls=calls, disp=self.disp, info=info)
        else:
            marker = PedMarker(rec=vcf_record, calls=calls, disp=self.disp,
                               info=info, family=self.family)
        return(marker)

    def fetch(self, chrom, start, end=None):
        if end is None:
            self.reader = self._reader.fetch(chrom, start, start + 1)
            try:
                return self.next()
            except StopIteration:
                return None
        self.reader = self._reader.fetch(chrom, start, end)
        return self


class Writer():
    """Output GBS marker data in VCF format."""
    def __init__(self, outstream, template, lineterminator='\n'):
        filename = template.filename
        disp = template.disp
        self.template = vcf.Reader(filename=filename)
        if template.family:
            for info in PEDINFO:
                self.template.infos[info.id] = info
        else:
            for info in INFO:
                self.template.infos[info.id] = info
        for format in FORMAT:
            self.template.formats[format.id] = format
        analysis = ''.join(("input_file=%s " % filename,
                            "disp_slope=%f " % disp['slope'],
                            "disp_intercept=%f" % disp['intercept']))
        self.template.metadata['GBStools'] = [analysis]
        self.writer = vcf.Writer(outstream, self.template, lineterminator)
        
    def write_record(self, marker):
        '''Write the marker data to outstream.'''
        # Update the vcf INFO field.
        for info_id, val in marker.info.items():
            if isinstance(val, float):
                marker.record.add_info(info_id, round(val, 3))
            elif isinstance(val, bool):
                if val is True:
                    marker.record.add_info(info_id, val)
            elif val is not None:
                marker.record.add_info(info_id, val)
        # Hash normalization factors and insert sizes, keyed by sample name.
        nf = {}
        ins = {}
        for call in marker.calls:
            nf[call.sample] = call.NF
            ins[call.sample] = call.INS
        # Update the vcf FORMAT field.
        if 'NF' not in marker.record.FORMAT:
            marker.record.add_format('NF')
        if 'INS' not in marker.record.FORMAT:
            marker.record.add_format('INS')
        if 'DC' not in marker.record.FORMAT:
            marker.record.add_format('DC')
        # Update the vcf sample data.
        for sample in marker.record.samples:
            ids = list(sample.data._fields)
            vals = list(iter(sample.data))
            if 'NF' not in ids:
                ids.append('NF')
                try:
                    vals.append(round(nf[sample.sample], 3))
                except:
                    vals.append(None)
            if 'INS' not in ids:
                ids.append('INS')
                try:
                    vals.append(int(ins[sample.sample]))
                except:
                    vals.append(None)
            if 'DC' not in ids:
                ids.append('DC')
                try:
                    dropout_count = marker.param['H1'][-1]['exp_phi'][sample.sample][2]
                    vals.append(round(dropout_count, 3))
                except:
                    vals.append(None)
            new_cls = make_calldata_tuple(ids)
            sample.data = new_cls._make(vals)
        # Write record to outstream.
        self.writer.write_record(marker.record)
        return(None)


class Marker():
    """Store data from a single GBS SNP marker and call EM functions."""
    def __init__(self, rec, calls, disp, info):
        self.record = rec
        self.calls = calls
        self.info = info
        self.param = {}
        self.lik_ratio = None
        # Initial dropout frequency.                                                                                                                                                                                                                                                                             
        dfreq = 0.01
        # Bool indicating allele data is missing.                                                                                                                                                                                                                                                                
        dp_mode = not bool([call.PL for call in calls if call.PL])
        if dp_mode:
            phi0 = [1 - dfreq, 0, dfreq]
            phi0_null = [1, 0, 0]
        else:
            try:
                af = min(0.9999, self.record.INFO['AF'][0])
            except:
                af = 0.01
            phi0 = [(1 - af) * (1 - dfreq), af * (1 - dfreq), dfreq]
            phi0_null = [1 - af, af, 0]
        dp = sum([call.DP for call in calls])
        missing = sum([call.DP == 0 for call in calls])
        if dp > 0:
            lambda0 = float(dp) / (len(calls) - missing)
            self.disp = disp['slope'] * lambda0 + disp['intercept']
            delta0 = max(float(missing) / len(calls), 0.01)
            fail = False
        else:
            lambda0 = None
            self.disp = None
            delta0 = None
            fail = True
        # Alternative hypothesis initial parameters.                                                                                                                                                                                                                                                             
        self.param['H1'] = [{'phi':phi0,
                             'lambda':lambda0,
                             'delta':delta0,
                             'fail':fail,
                             'loglik':None,
                             'exp_phi':None,
                             'exp_delta':None}]
        # Null hypothesis initial parameters.                                                                                                                                                                                                                                                                    
        self.param['H0'] = [{'phi':phi0_null,
                             'lambda':lambda0,
                             'delta':delta0,
                             'fail':fail,
                             'loglik':None,
                             'exp_phi':None,
                             'exp_delta':None}]

    def check_convergence(self, param, phi_tol=0.001, lamb_tol=0.1, delta_tol=0.005):
        '''Check convergence of EM.'''
        if param[-1]['fail']:
            converged = True
        elif len(param) > 1:
            phi_diff = (max(abs(param[-1]['phi'][1] - param[-2]['phi'][1]),
                            abs(param[-1]['phi'][2] - param[-2]['phi'][2])))
            lamb_diff = abs(param[-1]['lambda'] - param[-2]['lambda'])
            delta_diff = abs(param[-1]['delta'] - param[-2]['delta'])

            if phi_diff > phi_tol or lamb_diff > lamb_tol or delta_diff > delta_tol:
                converged = False
            else:
                converged = True
        else:
            converged = False
        return(converged)
        
    def update_param(self, param):
        '''Update the parameter estimates by EM (see GBStools notes).'''
        try:
            param_new = em.update(param[-1], self.calls, self.disp)
        except:
            param_new = param[-1].copy()
            param_new['fail'] = True
        return(param_new)

    def print_param(self, param_dict):
        '''Print out parameter estimates in a easy-to-read format'''
        try:
            print 'Frequency estimates (phi parameter) for alleles REF, ALT and `-` (non-cut allele masking REF or ALT): %s' % str(param_dict['phi'])
            print 'Coverage parameter (lambda) estimate: %f' % param_dict['lambda']
            print 'Digest failure parameter (delta) estimate: %f' % param_dict['delta']
            print 'Log-likelihood: %f' % param_dict['loglik']
            print 'EM failed: %s' % param_dict['fail']
        except:
            print 'Error parsing parameters'
        return(None)

    def likelihood_ratio(self):
        '''Null hypothesis: phi[2] == 0. Alt hypothesis: phi[2] > 0.'''
        try:
            lr = -2.0 * (self.param['H0'][-1]['loglik'] - 
                         self.param['H1'][-1]['loglik'])
        except:
            lr = None
        return(lr)
    
    def update_info(self):
        '''Update the INFO field for the output vcf.'''
        self.info['DLR'] = self.lik_ratio
        self.info['DFreq'] = self.param['H1'][-1]['phi'][2]
        self.info['AFH1'] = self.param['H1'][-1]['phi'][1]
        self.info['AFH0'] = self.param['H0'][-1]['phi'][1]
        self.info['LambdaH1'] = self.param['H1'][-1]['lambda']
        self.info['LambdaH0'] = self.param['H0'][-1]['lambda']
        self.info['DigestH1'] = self.param['H1'][-1]['delta']
        self.info['DigestH0'] = self.param['H0'][-1]['delta']
        self.info['IterationH1'] = len(self.param['H1'])
        self.info['IterationH0'] = len(self.param['H0'])
        self.info['EMFailH1'] = self.param['H1'][-1]['fail']
        self.info['EMFailH0'] = self.param['H0'][-1]['fail']
        return(None)
    
class PedMarker():
    """Store data from a single pedigree GBS SNP marker and call EM functions."""
    def __init__(self, rec, calls, disp, info, family):
        self.record = rec
        self.calls = calls
        self.info = info
        self.family = family
        self.param = {}
        self.lik_ratio = None
        dp = sum([call.DP for call in calls])
        missing = sum([call.DP == 0 for call in calls])
        if dp > 0:
            lambda0 = float(dp) / (len(calls) - missing)
            self.disp = disp['slope'] * lambda0 + disp['intercept']
            fail = False
        else:
            lambda0 = None
            self.disp = None
            fail = True
        for gt in em.trio_gt:
            self.param[gt] = [{'lambda':lambda0,
                               'fail':fail,
                               'loglik':None}]

    def check_convergence(self, param, lamb_tol=0.25):
        '''Check convergence of EM.'''
        if param[-1]['fail']:
            converged = True
        elif len(param) > 1:
            lamb_diff = abs(param[-1]['lambda'] - param[-2]['lambda'])
            if lamb_diff > lamb_tol:
                converged = False
            else:
                converged = True
        else:
            converged = False
        return(converged)

    def update_param(self, param, parental_gt, lamb_tol=0.25):
        '''Update the parameter estimates by EM (see GBStools notes).'''
        try:
            param_new = em.ped_update(param[-1], self.calls, self.disp, parental_gt)
        except:
            param_new = param[-1].copy()
            param_new['fail'] = True
        # Truncate the EM if the loglik is -inf to save computing time.
        if param_new['loglik'] == -float('Inf'):
            param_new['fail'] = True
        return(param_new)

    def likelihood_ratio(self):
        '''Null hypothesis: DCount = 0. Alt hypothesis DCount > 0.'''
        h0_lik = 0
        h1_lik = 0
        maxlik = max([param[-1]['loglik'] for param in self.param.values()])
        for geno in self.param:
            loglik = self.param[geno][-1]['loglik']
            if geno.father[2] == 0 and geno.mother[2] == 0:
                h0_lik += math.e**(loglik - maxlik) / 9
            else:
                h1_lik += math.e**(loglik - maxlik) / 27
        lr = -2.0 * (math.log(h0_lik) - math.log(h1_lik))
        return(lr)
    
    def update_info(self):
        '''Update the INFO field for the output vcf.'''
        # Dictionaries of parameter estimates for H1 and H0.
        paramH0 = {}
        paramH1 = {}
        for gt in em.trio_gt:
            if gt.mother[2] == 0 and gt.father[2] == 0:
                paramH0[gt] = self.param[gt]
            else:
                paramH1[gt] = self.param[gt]
        # Find the most likely parental genotypes for H1 and H0.
        gtH0 = max(paramH0.keys(), key=lambda x: paramH0[x][-1]['loglik'])
        gtH1 = max(paramH1.keys(), key=lambda x: paramH1[x][-1]['loglik'])
        # Format the genotypes.
        parentsH1 = (GT_FORMATTED[gtH1.father], GT_FORMATTED[gtH1.mother])
        parentsH0 = (GT_FORMATTED[gtH0.father], GT_FORMATTED[gtH0.mother])
        self.info['DLR'] = self.lik_ratio
        self.info['ParentsH1'] = "%s,%s" % parentsH1
        self.info['ParentsH0'] = "%s,%s" % parentsH0
        self.info['LambdaH1'] = self.param[gtH1][-1]['lambda']
        self.info['LambdaH0'] = self.param[gtH0][-1]['lambda']
        self.info['IterationH1'] = len(self.param[gtH1])
        self.info['IterationH0'] = len(self.param[gtH0])
        self.info['EMFailH1'] = self.param[gtH1][-1]['fail']
        self.info['EMFailH0'] = self.param[gtH0][-1]['fail']
        return(None)


class _Samfile():
    """ Class for generating ''Pileup'' objects from pysam ''Samfile'' objects. """
    def __init__(self, bam, sample):
        self.sample = sample
        self.bam = pysam.Samfile(bam, 'rb')

    def pileup(self, chrom, pos, ref, alt):
        """ Return ''CallData'' object containing DP, PL etc."""
        # Generate pysam ''Pileup'' object.
        pileup = self.bam.pileup(chrom, pos, pos + 1, truncate=True)
        # Extract data from pileup.
        data = _PileupData(pileup, ref, alt)
        call_data = CallData(self.sample, **data.data)
        return(call_data)


class _PileupData():
    """ Class for extracting read information from single loci in ''Pileup'' objects. """
    def __init__(self, pileup, ref, alt, maxcovg=250, offset=33):
        # The pysam ''PileupProxy'' for this sample.
        self.pileup = pileup
        self.data = {}
        try:
            # Get the pysam ''PileupColumn''.
            pileupcol = self.pileup.next()
            # Get list of pysam ''PileupRead'' objects.
            reads = [read for read in pileupcol.pileups if read.alignment.mapq > 0]
            self.reads = reads[:maxcovg]
            self.data['DP'] = len(self.reads)
        except:
            self.reads = []
            self.data['DP'] = 0
        # Calculate PL.
        try:
            self.ref = ref
            self.alt = str(alt)
            self.data['PL'] = self.calculate_pl(offset)
        except:
            self.alt = None
            self.data['PL'] = None
        # Extract info from read tags.
        inserts, self_rs, mate_rs = self.extract_tags()
        self.data['inserts'] = inserts
        if inserts:
            self.data['INS'] = median(inserts)
        else:
            self.data['INS'] = None
        self.data['self_rs'] = self_rs
        self.data['mate_rs'] = mate_rs

    def extract_tags(self):
        '''Extract insert and enzyme info from read tags (see annotate_bam.py).'''
        tags = [dict(read.alignment.tags) for read in self.reads]
        inserts = [tag['Z0'] for tag in tags if 'Z0' in tag]
        self_rs = [tag['Z2'] for tag in tags if 'Z2' in tag]
        mate_rs = [tag['Z4'] for tag in tags if 'Z4' in tag]
        return(inserts, self_rs, mate_rs)

    def calculate_insert_med(self):
        '''Calculate insert size before read trimming.'''
        if self.inserts:
            ins_med = median(self.inserts)
            ins_mad = median([abs(ins_med - insert) for insert in self.inserts])
        else:
            ins_med = None
            ins_mad = None
        return(ins_med, ins_mad)

    def calculate_pl(self, offset):
        '''Calculate genotype likelihoods and allele depth from reads.'''
        homref_lik = 0
        het_lik = 0
        homnonref_lik = 0
        for read in self.reads:
            base = read.alignment.seq[read.qpos]
            # Calculate Pr(ref is true | base is miscalled)
            pr_ref = CONFUSION_MATRIX[self.ref][self.alt]
            # Calculate Pr(alt is true | base is miscalled)
            pr_alt = CONFUSION_MATRIX[self.alt][self.ref]
            # Calculate the base call error rate, epsilon.
            qual = read.alignment.qual[read.qpos]
            epsilon = 10**(-(ord(qual) - offset) / 10.0)
            if base == self.ref:
                homref_lik += math.log(1 - epsilon, 10)
                het_lik += math.log((1 - epsilon * (1 + pr_alt)) / 2.0, 10)
                homnonref_lik += math.log(epsilon * pr_alt, 10)
            elif base == self.alt:
                homref_lik += math.log(epsilon * pr_ref, 10)
                het_lik += math.log((1 - epsilon * (1 + pr_ref)) / 2.0, 10)
                homnonref_lik += math.log(1 - epsilon, 10)
        lik = (homref_lik, het_lik, homnonref_lik)
        if sum(lik) < 0:
            # Normalized the likelihoods.
            maxlik = max(lik)
            pl = [-10 * (l - maxlik) for l in lik]
        else:
            pl = None
        return(pl)


class CallData():
    """Class for storing data for an individual sample."""
    def __init__(self, sample, **kwargs):
        self.sample = sample
        dp = kwargs.pop('DP', 0)
        if dp is None:
            self.DP = 0
        else:
            self.DP = dp
        pl = kwargs.pop('PL', None)
        if pl:
            self.PL = [int(i) for i in pl]
        else:
            self.PL = None
        nf = kwargs.pop('NF', 1.0)
        try:
            self.NF = float(nf)
        except:
            self.NF = 1.0
        self.inserts = kwargs.pop('inserts', [])
        self.INS = kwargs.pop('INS', None)
        self.self_rs = kwargs.pop('self_rs', [])
        self.mate_rs = kwargs.pop('mate_rs', [])
        self.is_father = False
        self.is_mother = False
        self.is_child = False
