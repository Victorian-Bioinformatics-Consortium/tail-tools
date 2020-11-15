
import collections

from nesoni import config, io, grace

import sys


State = collections.namedtuple('State','state score a_start a_end') 


@config.help(
'Clip low quality sequence and poly-A runs from the end of colorspace reads.',
"""\
The run of 0s can contain up to one fifth other colors, to allow for \
sequencing errors.

Reads should be in CSFASTQ format.
""")
@config.String_flag('sample', 'Sample name (for logging of statistics).')
@config.Int_flag('quality', 'Minimum quality.')
#@config.String_flag('adaptor', 'Adaptor sequence expected after poly-A tail (basespace only).')
@config.Int_flag('length', 'Minimum length.')
@config.Bool_flag('debug', 'Show detected poly-A region and adaptor location in each read.')
@config.Main_section('filenames', 'Input FASTQ files.')
class Clip_runs_colorspace(config.Action_with_prefix):
    sample = 'sample'
    quality = 20
    length = 25
    debug = False
    filenames = [ ]

    def run(self):
        min_quality = chr(33+self.quality)

        with io.open_possibly_compressed_writer(self.prefix+'.csfastq.gz') as out_file:        
            n = 0
            n_discarded = 0
            n_clipped = 0
            total_before = 0
            total_clipped = 0
            for filename in self.filenames:
                for name, seq, qual in io.read_sequences(filename, qualities='required'):
                    
                    score = 0
                    start = 0
                    for i in xrange(len(seq)-1):
                        if qual[i] >= min_quality:
                            if seq[i+1] == '0':
                                score += 1
                            else:
                                score = max(0, score-4)
                                if not score: start = i+2
                    
                    n += 1
                    total_before += len(seq)
                
                    if start > self.length+1:
                        if start < len(seq):
                            n_clipped += 1
                            total_clipped += len(seq)-start
                        
                        print >> out_file, '@'+name
                        print >> out_file, seq[:start]
                        print >> out_file, '+'
                        print >> out_file, qual[:start-1]
                    else:
                        n_discarded += 1
        
        self.log.datum(self.sample,'reads',n)
        if n:
            self.log.datum(self.sample,'mean length before poly-A clipping',float(total_before)/n)
        self.log.datum(self.sample,'reads discarded as too short after poly-A clipping',n_discarded)
        self.log.datum(self.sample,'reads poly-A clipped and kept',n_clipped)
        if n_clipped:
            self.log.datum(self.sample,'mean length clipped',float(total_clipped)/n_clipped)





@config.help(
'Clip low quality sequence and poly-A runs from the end of basespace reads.',
"""\
Looks for a run of As, followed by an adaptor sequence, followed by anything.

A good quality region is found, containing 90% bases with quality at least --clip-quality. The run of As must lie withing this region, but the adaptor sequence may extend beyond this. Up to 20% errors are allowed in the poly(A) and adaptor sequence.

Reads should be in FASTQ format.
""")
@config.String_flag('sample', 'Sample name (for logging of statistics).')
@config.Int_flag('clip_quality', 'Sequence is clipped to a region containing 90% of bases with at least this quality. G bases are ignored for this purpose, since two-color sequencing will produce high quality Gs for pure black.')
@config.Int_flag('clip_penalty', 'One low quality base is made up for by this many good quality bases.')
@config.Int_flag('a_mismatch_penalty', 'Penalty to score for non-A when matching poly(A).')
@config.Int_flag('adaptor_mismatch_penalty', 'Penalty to score for adaptor mismatch when matching adaptor.')
#@config.Int_flag('ignore_quality', 'When calling poly(A) and adaptor, ignore bases below this quality. This may be lower than --clip-quality.')
@config.Int_flag('min_score', 'Minimum score to call a poly(A) tail, essentially number of As+adaptor bases matched.')
@config.String_flag('adaptor', 'Adaptor sequence expected after poly-A tail (basespace only).')
@config.Int_flag('length', 'Minimum length.')
@config.Int_flag('only', 'Only use first NNN reads (for debugging). 0 means use all reads.')
@config.Bool_flag('debug', 'Show detected poly-A region and adaptor location in each read.')
@config.Main_section('filenames', 'Input FASTQ files.')
class Clip_runs_basespace(config.Action_with_prefix):
    sample = 'sample'
    adaptor = 'GATCGGAAGAGCACACGTCTGAACTCCAGTCAC'    
    clip_quality = 0
    clip_penalty = 4
    #ignore_quality = 0
    a_mismatch_penalty = 4
    adaptor_mismatch_penalty = 4 
    min_score = 10
    length = 20
    debug = False
    only = 0
    filenames = [ ]

    def run(self):
        """
        
        <sequence> <poly-A> <adaptor> <anything>
        
        """
        clip_quality = chr(33+self.clip_quality)
        #ignore_quality = chr(33+self.ignore_quality)
        
        with io.open_possibly_compressed_writer(self.prefix+'.fastq.gz') as out_file, \
             io.open_possibly_compressed_writer(self.prefix+'.clips.gz') as out_clips_file:
            print >> out_clips_file, '#Read\tread length\tpoly-A start\tpoly-A end\tpoly-A start, ignoring adaptor\tpoly-A end, ignoring adaptor\tadaptor bases matched'
             
            n = 0
            n_discarded = 0
            n_clipped = 0
            total_before = 0
            total_clipped = 0

            for filename in self.filenames:
                for name, seq, qual in io.read_sequences(filename, qualities='required'):

                    # Find a good point to clip the reads so that
                    # most of the bases have good quality.
                    #
                    # This is primarily for use with two-color sequencing
                    # by newer Illumina machines such as NovaSeq.
                    #
                    # Gs are not examined for quality as both colors
                    # off is a G, and we see "high quality" Gs beyond the
                    # end of the fragment. 
                    if self.clip_quality <= 0:
                        good_quality_end = len(seq)
                    else:
                        goodness_score = 0
                        best_goodness_score = 0
                        good_quality_end = 0
                        i = 0
                        while True:
                            if goodness_score > best_goodness_score:
                                best_goodness_score = goodness_score
                                good_quality_end = i
                            
                            if i >= len(seq):
                                break

                            if seq[i] != 'G':
                                if qual[i] >= clip_quality:
                                    goodness_score += 1
                                else:
                                    goodness_score -= self.clip_penalty
                            i += 1
                            
                    
                    best_score = self.min_score-1
                    best_a_start = good_quality_end
                    best_a_end = good_quality_end
                    best_adaptor_bases = 0
                    best_aonly_score = 0
                    best_aonly_start = good_quality_end
                    best_aonly_end = good_quality_end
                    
                    # Consider each possible start position for the poly(A)
                    for a_start in xrange(good_quality_end):
                        if a_start and seq[a_start-1] == 'A': continue
                        
                        # Consider each possible end position for the poly(A)
                        a_end = a_start
                        aonly_score = 0
                        while True:
                            if aonly_score > best_aonly_score:
                                best_aonly_score = aonly_score
                                best_aonly_start = a_start
                                best_aonly_end = a_end
                            
                            
                            # The poly(A) should be followed by adaptor,
                            score = aonly_score
                            adaptor_bases = 0
                            i = a_end
                            abort_score = best_score-len(self.adaptor)
                            abort_i = min(good_quality_end, a_end+len(self.adaptor))
                            while score >= abort_score:
                                #if (score > best_score and 
                                #    (i >= good_quality_end or i >= a_end+len(self.adaptor))):
                                if score > best_score:
                                    best_score = score
                                    best_a_start = a_start
                                    best_a_end = a_end
                                    best_adaptor_bases = adaptor_bases
                            
                                if i >= abort_i:
                                    break
                            
                                if seq[i] == self.adaptor[i-a_end]:
                                    score += 1
                                    adaptor_bases += 1
                                else:
                                    score -= self.adaptor_mismatch_penalty
                                i += 1
                                
                            #if a_end >= len(seq): break
                            
                            # Modified 2018-03-21
                            # poly(A) tail only within good quality region.
                            #if a_end >= good_quality_end: break
                            #if qual[a_end] >= ignore_quality:
                            #    if seq[a_end] == 'A':
                            #        aonly_score += 1
                            #    else:
                            #        aonly_score -= 4
                            #        if aonly_score <= 0: break

                            if a_end >= good_quality_end: break

                            if seq[a_end] == 'A':
                                aonly_score += 1
                            else: #if qual[a_end] >= ignore_quality:
                                aonly_score -= self.a_mismatch_penalty
                            #else:
                            #    aonly_score -= 1                       

                            a_end += 1
                    
                    # 2018-03-21 
                    # Look for tail starting after good quality,
                    # however don't call a tail if starts after good quality 
                    ## Disabled: tail must also be within good quality region
                    #if best_a_start > good_quality_end:
                    #    best_a_start = good_quality_end
                    #    best_a_end = good_quality_end
                    #    best_adaptor_bases = 0
                    #    best_score = 0

                    a_start = best_a_start
                    a_end = best_a_end
                    adaptor_bases = best_adaptor_bases
                    aonly_start = best_aonly_start
                    aonly_end = best_aonly_end                    
                        
                    if self.debug: # and a_end == a_start and a_end < len(seq)-10:        
                        print name
                        print ''.join( 
                            ('C' if item<clip_quality else ' ') 
                            for item in qual )
                        print '-' * good_quality_end
                        print seq
                        print ' '*a_start + 'A'*(a_end-a_start) + self.adaptor + ".%d %d"%(adaptor_bases,best_score)
                        #print ' '*aonly_start + 'A'*(aonly_end-aonly_start) + "."
                        print 
                        sys.stdout.flush()

                    n += 1
                    total_before += len(seq)

                    # 0 - sequence name
                    # 1 - sequence length
                    # 2 - poly(A) start
                    # 3 - poly(A) end
                    # (4 - best run of As start, for debugging the need to detect adaptor seq)
                    # (5 - best run of As end)
                    # 6 - number of adaptor bases matched
                    print >> out_clips_file, '%s\t%d\t%d\t%d\t%d\t%d\t%d' % (name, len(seq) , a_start, a_end, aonly_start, aonly_end,  adaptor_bases)
                    
                    if a_start >= self.length:
                        if a_start < len(seq):
                            n_clipped += 1
                            total_clipped += a_start
                    
                        print >> out_file, '@'+name
                        print >> out_file, seq[:a_start]
                        print >> out_file, '+'
                        print >> out_file, qual[:a_start]
                    else:
                        n_discarded += 1
                    
                    if n%10000 == 0: 
                        grace.status('Clip-runs ' + self.sample + ' ' + grace.pretty_number(n)) # + ' (' + grace.pretty_number(len(dstates)) + ' dstates)')
                    
                    # Option to do a quick subsample
                    if self.only and self.only <= n:
                        break
        
        grace.status('')
        
        self.log.datum(self.sample,'reads',n)
        if n:
            self.log.datum(self.sample,'mean length before poly-A/adaptor clipping',float(total_before)/n)
        self.log.datum(self.sample,'reads discarded as too short after poly-A/adaptor clipping',n_discarded)
        self.log.datum(self.sample,'reads poly-A/adaptor clipped and kept',n_clipped)
        if n_clipped:
            self.log.datum(self.sample,'mean length clipped',float(total_clipped)/n_clipped)




if __name__ == '__main__':
    config.shell_run(Clip_runs(), sys.argv[1:], sys.executable + ' ' + __file__)
