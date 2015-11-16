
import itertools, collections, math, os.path

import nesoni
from nesoni import annotation, sam, span_index, config, working_directory, workspace, io, runr, reporting, selection, legion

import cPickle as pickle

def str_na(value):
    if value is None:
        return "NA"
    else:
        return str(value)


@config.help("""
Create file to be used by "aggregate-tail-lengths:".

Reads are aligned to "parts" features. A parent is then sought of type "types", possibly several levels up, or possibly zero levels up.

If part features have a "max_extension" attribute, this is respected when extending them. Typically this is used to avoid extending into a following CDS.
""")
@config.String_flag('annotations', 'Filename containing annotations. Defaults to annotations in reference directory.')
@config.String_flag('types', 'Comma separated list of feature types to use.')
@config.String_flag('parts', 'Comma separated list of feature types that make up features. Defaults to types if blank.')
@config.Int_flag('extension', 'How far downstrand of the given annotations a read or peak belonging to a gene might be.')
@config.Positional('working_dir', 'Working directory to use as input.')
class Tail_count(config.Action_with_prefix):
     annotations = None
     types = 'gene'
     parts = 'exon'
     working_dir = None
     
     extension = None
     
     ##Memory intensive, hack to run with reduced parallelism
     ## as nesoni doesn't have any memory usage management, only core usage
     #def cores_required(self):
     #    return min(4, legion.coordinator().get_cores())

     def run(self):
         assert self.extension is not None, '--extension must be specified'
     
         #workspace = self.get_workspace()
         workspace = working_directory.Working(self.working_dir, must_exist=True)
         if self.annotations == None:
             reference = workspace.get_reference()
             annotations_filename = reference.annotations_filename()
         else:
             annotations_filename = self.annotations
         
         types = [ item.lower() for item in self.types.split(',') ]
         
         parts = self.parts or self.types 
         parts = [ item.lower() for item in parts.split(',') ]
         
         
         all_annotations = list(annotation.read_annotations(annotations_filename))
         annotation.link_up_annotations(all_annotations)
         for item in all_annotations: 
             item.primary = None
     
         annotations = [ 
             item 
             for item in all_annotations
             if item.type.lower() in types
         ]
         
         part_annotations = [ ]
         seen = set()
         queue = [ (item,item) for item in annotations ]
         while queue:
             primary, item = queue.pop()
             if item.type.lower() in parts:
                 assert item.primary is None, "Feature with multiple parents"
                 item.primary = primary
                 key = (id(primary),item.start,item.end,item.seqid,item.strand)
                 # Ignore duplicate exons (many isoforms will have the same exons)
                 if key not in seen:
                     seen.add(key)
                     part_annotations.append(item)
             queue.extend( (primary, item2) for item2 in item.children )
         
         del seen
         del all_annotations
         
         self.log.log('%d annotations\n' % len(annotations))
         self.log.log('%d part annotations\n' % len(part_annotations))
         
         assert annotations, 'No annotations of specified types in file'
         
         for item in part_annotations:
             this_extension = self.extension
             if "max_extension" in item.attr:
                 this_extension = min(this_extension,int(item.attr["max_extension"]))
                 
             if item.strand >= 0:
                 item.tail_pos = item.end
                 item.end += this_extension
             else:
                 item.tail_pos = item.start
                 item.start -= this_extension
         
         for item in annotations:    
             item.hits = [] # [ (rel_start, rel_end, tail_length) ]
         
         index = span_index.index_annotations(part_annotations)
         
         for read_name, fragment_alignments, unmapped in sam.bam_iter_fragments(workspace/'alignments_filtered.bam'):
             for fragment in fragment_alignments:
                 start = min(item.pos-1 for item in fragment)
                 end = max(item.pos+item.length-1 for item in fragment)
                 alignment_length = end-start
                 strand = -1 if fragment[0].flag&sam.FLAG_REVERSE else 1
                 fragment_feature = annotation.Annotation(
                     seqid=fragment[0].rname,
                     start=start,
                     end=end,
                     strand=strand
                     )
                 
                 if strand >= 0:
                     tail_pos = end
                 else:
                     tail_pos = start
                 
                 tail_length = 0
                 adaptor_bases = 0
                 for item in fragment[0].extra:
                     if item.startswith('AN:i:'):
                         tail_length = int(item[5:])
                     elif item.startswith('AD:i:'):
                         adaptor_bases = int(item[5:])
                 
                 hits = index.get(fragment_feature, same_strand=True)
                 if hits:
                     gene = min(hits, key=lambda gene: (abs(tail_pos - gene.tail_pos), gene.primary.get_id()))
                         # Nearest by tail_pos
                         # failing that, by id to ensure a deterministic choice
                         
                     if strand > 0:
                         rel_start = start - gene.start
                         rel_end = end - gene.start
                     else:
                         rel_start = gene.end - end
                         rel_end = gene.end - start
                     
                     gene.primary.hits.append( (rel_start,rel_end,tail_length,adaptor_bases) )

         for item in annotations:
             del item.parents
             del item.children
             del item.primary

         f = io.open_possibly_compressed_writer(self.prefix + '.pickle.gz')
         pickle.dump((workspace.name, workspace.get_tags(), annotations), f, pickle.HIGHEST_PROTOCOL)
         f.close()
         


@config.help(
'Aggregate data collected by "tail-lengths:" and produce various CSV tables.',
"""\
"""
)
@config.Int_flag('saturation', 
     'Duplicate start position saturation level. '
     'Reads that start at the same position will only '
     'count as up to this many. Zero for no staturation.'
     )
@config.Int_flag('tail',
     'Minimum tail length to count as having a tail.'
     )
@config.Int_flag('adaptor',
     'Minimum number of adaptor bases required, 0 for no filtering.'
     )
@config.Main_section('pickles')
class Aggregate_tail_counts(config.Action_with_output_dir):             
    saturation = 0
    tail = 4
    adaptor = 0
    pickles = [ ]
     
    #Memory intensive, don't run in parallel
    def cores_required(self):
        return legion.coordinator().get_cores()
         
    def run(self):
        work = self.get_workspace()
        
        data = [ ]
        names = [ ]
        sample_tags = [ ]
        
        #TODO: store max_length separately, avoid loading twice
        
        max_length = 1
        for i, item in enumerate(self.pickles):
            f = io.open_possibly_compressed_file(item)
            name, tags, datum = pickle.load(f)
            f.close()
            #data.append(datum)
            names.append(name)
            sample_tags.append(tags)
            
            try:
                max_length = max(max_length, max( 
                    item[2] #tail_length
                    for feature in datum
                    for item in feature.hits
                    ) + 1)
            except ValueError:
                pass
            
            if i == 0:
               annotations = datum
               for item in annotations:
                   del item.hits
        
        del datum
        
        self.log.log("Maximum tail length %d\n" % max_length)

        data = [ ]                    
        for i, item in enumerate(self.pickles):
            f = io.open_possibly_compressed_file(item)
            name, tags, sample = pickle.load(f)
            f.close()
            data.append(sample)            
            
            n_alignments = 0
            n_duplicates = 0
            n_good = 0
            for feature in sample:
                feature.total_count = 0.0
                feature.tail_counts = [ 0.0 ] * max_length
                
                buckets = collections.defaultdict(list)
                for item in feature.hits:
                    rel_start,rel_end,tail_length,adaptor_bases = item
                    buckets[ (rel_start,rel_end) ].append((tail_length,adaptor_bases))
                del feature.hits
                
                for item in buckets.values():
                    n_alignments += len(item)
                    n_good += 1
                    if self.saturation < 1 or len(item) <= self.saturation:
                        weight = 1.0
                    else:
                        weight = float(self.saturation) / len(item)
                        n_duplicates += len(item)
                    for tail_length, adaptor_bases in item:
                        feature.total_count += weight
                        if adaptor_bases >= self.adaptor:
                            feature.tail_counts[tail_length] += weight                

            self.log.datum(names[i], 'Alignments to features', n_alignments)
            if self.saturation >= 1:
                self.log.datum(names[i], 'Proportion of alignments with duplicate start and end position', float(n_duplicates)/max(1,n_alignments))
                self.log.datum(names[i], 'Alignments to features after deduplication', n_good)
                
        
        counts = [ ]  # [feature][sample](total_count, [taillength])
        
        for item in data: 
            assert len(item) == len(data[0])
        for row in itertools.izip(*data):
            this_counts = [ (item.total_count, item.tail_counts) for item in row ]
            counts.append(this_counts)
        
        n_features = len(counts)
        n_samples = len(data)
        
        sample_n = [ [0.0]*n_samples for i in xrange(n_features) ]        # [feature][sample]  Total count
        sample_n_tail = [ [0.0]*n_samples for i in xrange(n_features) ]   # [feature][sample]  Polya count
        sample_prop = [ [None]*n_samples for i in xrange(n_features) ]    # [feature][sample]  Proportion of reads with tail (deprecated)
        sample_tail = [ [None]*n_samples for i in xrange(n_features) ]    # [feature][sample]  Mean tail length in each sample
        sample_sd_tail = [ [None]*n_samples for i in xrange(n_features) ] # [feature][sample]  Std dev tail length in each sample
        sample_total_tail = [ [0.0]*n_samples for i in xrange(n_features) ]
        
        sample_quantile_tail = collections.OrderedDict( 
            (item, [ [None]*n_samples for i in xrange(n_features) ]) 
            for item in [25,50,75,100]
            )
        
        overall_n = [ 0.0 ]*n_features       # [feature]          Overall count
        overall_prop = [ None ]*n_features   # [feature]          Overall proportion with tail
        overall_tail = [ None ]*n_features   # [feature]          Overall mean tail length
        overall_n_tail = [ 0.0 ]*n_features  # [feature]          Overall polya count
        for i, row in enumerate(counts):
            for j, (this_this_n, item) in enumerate(row):
                sample_n[i][j] = this_this_n
                sample_n_tail[i][j] = sum(item[self.tail:])
                sample_total_tail[i][j] = sum( item[k]*k for k in xrange(self.tail,max_length) )

                if sample_n[i][j] >= 1:
                    sample_prop[i][j] = float(sample_n_tail[i][j])/sample_n[i][j]
                
                if sample_n_tail[i][j] >= 1:
                    sample_tail[i][j] = float(sample_total_tail[i][j])/sample_n_tail[i][j]
                
                    for quantile in sample_quantile_tail:
                        counter = sample_n_tail[i][j] * quantile / 100.0
                        for k in xrange(self.tail, max_length):
                            counter -= item[k]
                            if counter <= 0: break
                        sample_quantile_tail[quantile][i][j] = k
                
                if sample_n_tail[i][j] >= 2:
                    sample_sd_tail[i][j] = math.sqrt(
                        float(sum( item[k]*((k-sample_tail[i][j])**2) for k in xrange(self.tail,max_length) ))
                        / (sample_n_tail[i][j]-1)
                        )
                    
            overall_n[i] = sum(sample_n[i])
            overall_n_tail[i] = sum(sample_n_tail[i])
            if overall_n[i] >= 1:
                overall_prop[i] = float(sum(sample_n_tail[i]))/overall_n[i]
            if overall_n_tail[i] >= 1:
                overall_tail[i] = float(sum(sample_total_tail[i]))/overall_n_tail[i]
             
        for i, name in enumerate(names):
            this_total = sum( item[i] for item in sample_total_tail )
            this_n = sum( item[i] for item in sample_n_tail )
            if this_n:
                self.log.datum(name, 'Average poly-A tail', float(this_total)/this_n)
                
        for i, name in enumerate(names):
            this_total = sum( item[i] for item in sample_n_tail )
            this_n = sum( item[i] for item in sample_n )
            if this_n:
                self.log.datum(name, 'Average proportion of reads with tail', float(this_total)/this_n)
        
        with open(work/'features-with-data.gff','wb') as f:
            annotation.write_gff3_header(f)
            for i, item in enumerate(annotations):
                item.attr['reads'] = str(overall_n[i])
                item.attr['reads_with_tail'] = str(overall_n_tail[i])
                item.attr['mean_tail'] = '%.1f'%overall_tail[i] if overall_tail[i] else 'NA'
                item.attr['proportion_with_tail'] = '%.2f'%overall_prop[i] if overall_prop[i] else 'NA'
                
                if overall_tail[i] is None:
                    item.attr['color'] = '#444444'
                else:
                    a = (overall_tail[i]-self.tail)/max(1,max_length-self.tail)
                    item.attr['color'] = '#%02x%02x%02x' % (int(a*255),int((1-abs(a*2-1))*255),255-int(a*255))
                #item.attr['color'] = ...                
                print >> f, item.as_gff()
        
        
        comments = [ '#Counts' ] + [
            '#sampleTags='+','.join(tags)
            for tags in sample_tags
            ] + [
            '"Tail_count" group is number of reads with tail',
            '"Tail" group is mean tail per sample',
            '"Proportion" group is proportion of reads with tail',
            ]
            
        have_biotype = any("Biotype" in item.attr for item in annotations)
        have_parent = any("Parent" in item.attr for item in annotations)
        have_relation = any("Relation" in item.attr for item in annotations)

        def counts_iter():
            for i in xrange(n_features):
                row = collections.OrderedDict()
                row['Feature'] = annotations[i].get_id()
                for j in xrange(n_samples):
                    row[('Count',names[j])] = '%d' % sample_n[i][j]

                row[('Annotation','Length')] = annotations[i].end - annotations[i].start
                row[('Annotation','gene')] = annotations[i].attr.get('Name','')
                row[('Annotation','product')] = annotations[i].attr.get('Product','')
                if have_biotype:
                    row[('Annotation','biotype')] = annotations[i].attr.get('Biotype','')
                if have_parent:
                    row[('Annotation','parent')] = annotations[i].attr.get('Parent','')
                if have_relation:
                    row[('Annotation','relation')] = annotations[i].attr.get('Relation','')
                
                row[('Annotation','chromosome')] = str(annotations[i].seqid)
                row[('Annotation','strand')] = str(annotations[i].strand)
                row[('Annotation','start')] = str(annotations[i].start+1)
                row[('Annotation','end')] = str(annotations[i].end)
                
                row[('Annotation','reads')] = str(overall_n[i])
                row[('Annotation','reads-with-tail')] = str(overall_n_tail[i])
                row[('Annotation','mean-tail')] = str_na(overall_tail[i])
                row[('Annotation','proportion-with-tail')] = str_na(overall_prop[i])
                for j in xrange(n_samples):
                    row[('Tail_count',names[j])] = '%d' % sample_n_tail[i][j]
                for j in xrange(n_samples):
                    row[('Tail',names[j])] = str_na(sample_tail[i][j])
                for j in xrange(n_samples):
                    row[('Tail_sd',names[j])] = str_na(sample_sd_tail[i][j])
                
                for quantile in sample_quantile_tail:
                    for j in xrange(n_samples):
                        row[('Tail_quantile_%d'%quantile,names[j])] = str_na(sample_quantile_tail[quantile][i][j])                    
                
                for j in xrange(len(names)):
                    row[('Proportion',names[j])] = str_na(sample_prop[i][j])
                yield row
        io.write_csv(work/'counts.csv', counts_iter(), comments=comments)
        
        
        def write_csv_matrix(filename, matrix):
            def emitter():
                for i in xrange(n_features):
                    row = collections.OrderedDict()
                    row["Feature"] = annotations[i].get_id()
                    for j in xrange(n_samples):
                        row[names[j]] = str_na(matrix[i][j])
                    yield row
            io.write_csv(filename, emitter())
            
        write_csv_matrix(work/'read_count.csv', sample_n)
        write_csv_matrix(work/'tail_count.csv', sample_n_tail)
        write_csv_matrix(work/'tail.csv', sample_tail)
        write_csv_matrix(work/'tail_sd.csv', sample_sd_tail)
        for quantile in sample_quantile_tail:
            write_csv_matrix(work/('tail_quantile_%d.csv'%quantile), sample_quantile_tail[quantile])


        def raw_columns():
            for i in xrange(n_samples):
                row = collections.OrderedDict()
                row['Sample'] = names[i]
                for j in xrange(max_length):
                    row['length-%d' % j] = str(i*max_length+j+1) #For R+, so 1 based
                yield row
        io.write_csv(work/'raw-columns.csv', raw_columns())

        #Somewhat inefficient        
        def raw():
            for i in xrange(n_features):
                row = collections.OrderedDict()
                row['Feature'] = annotations[i].get_id()
                for j in xrange(n_samples):
                    for k in xrange(max_length):
                        row['%d %s' % (k,names[j])] = str( counts[i][j][1][k] )
                yield row
        io.write_csv(work/'raw.csv', raw())
        
        def pooled():
            for i in xrange(n_features):
                row = collections.OrderedDict()
                row['Feature'] = annotations[i].get_id()
                for j in xrange(max_length):
                    row[str(j)] = str( sum( counts[i][k][1][j] for k in xrange(n_samples) ) )
                yield row
        io.write_csv(work/'pooled.csv', pooled())




#@config.help(
#    'Create a spreadsheet containing various statistics, using the output of "aggregate-tail-lengths:"',
#    'The prefix should be the same as that given to "aggregate-tail-lengths:".'
#)
#class Tail_stats(config.Action_with_prefix, runr.R_action):
#    def state_filename(self):
#        return self.prefix + '_tail_stats.state'
#
#    def log_filename(self):
#        if self.prefix is None: return None
#        return self.prefix + '_tail_stats_log.txt'
#        
#    script = r"""
#    
#    library(nesoni)
#
#    # === Load data ==
#    
#    data <- read.grouped.table(sprintf('%s-counts.csv',prefix),require=c('Count','Annotation'))
#    annotation <- data$Annotation
#    expression <- voom( as.matrix(data$Count), normalize.method='quantile' )$E
#    
#    raw <- as.matrix(read.grouped.table(sprintf('%s-raw.csv',prefix))$All)
#    
#    cols <- as.matrix(read.grouped.table(sprintf('%s-raw-columns.csv',prefix))$All)   
#    
#    row.names <- rownames(annotation)
#    n.samples <- nrow(cols)
#    n.genes <- nrow(raw)
#    n.lengths <- ncol(cols)
#    lengths <- basic.seq(n.lengths)-1
#
#
#    # === Calculate mean tail lengths for each sample ===
#    
#    # Omit columns for tail length 0,1,2,3    
#    skip <- 4
#    good.cols <- cols[,(skip+1):n.lengths,drop=FALSE]
#    good.lengths <- lengths[(skip+1):n.lengths]
#    
#    totals <- matrix(nrow=n.genes,ncol=n.samples) 
#    x.totals <- matrix(nrow=n.genes,ncol=n.samples) 
#    xx.totals <- matrix(nrow=n.genes,ncol=n.samples) 
#    means <- matrix(nrow=n.genes,ncol=n.samples)
#    sds <- matrix(nrow=n.genes,ncol=n.samples)
#    colnames(totals) <- rownames(cols)
#    rownames(totals) <- row.names
#    colnames(means) <- rownames(cols)
#    rownames(means) <- row.names
#    colnames(sds) <- rownames(cols)
#    rownames(sds) <- row.names
#    for(c in basic.seq(n.samples)) {
#        totals[,c] <- rowSums(raw[,good.cols[c,],drop=FALSE])
#        x.totals[,c] <- rowSums( t(t(raw[,good.cols[c,],drop=FALSE]) * good.lengths) )
#        xx.totals[,c] <- rowSums( t(t(raw[,good.cols[c,],drop=FALSE]) * (good.lengths*good.lengths) ) )
#
#        means[,c] <- x.totals[,c] / totals[,c]
#        sds[,c] <- sqrt( totals[,c]/(totals[,c]-1) * (xx.totals[,c]/totals[,c] - means[,c]*means[,c]) )
#        
#        means[totals[,c] < 1,c] <- NA
#        sds[totals[,c] < 2,c] <- NA
#    }
#
#
#    grand.totals <- rowSums(totals)
#    grand.x.totals <- rowSums(x.totals)
#    grand.xx.totals <- rowSums(xx.totals)
#    grand.means <- grand.x.totals / grand.totals
#    grand.sds <- sqrt( grand.totals/(grand.totals-1) * (grand.xx.totals/grand.totals - grand.means*grand.means) )
#    grand.means[ grand.totals < 1 ] <- NA
#    grand.sds[ grand.totals < 2 ] <- NA
#    pool.frame <- data.frame(
#        "Reads" = rowSums(raw),
#        "Tailed reads" = grand.totals,
#        "Mean tail length" = grand.means,
#        "StdDev tail length" = grand.sds,
#        row.names = row.names,
#        check.names = FALSE,
#        check.rows = FALSE
#    )
#
#    # === Output ===
#    
#    write.grouped.table(
#        list(
#            "Annotation" = annotation,
#            "Count" = as.data.frame( data$Count ),
#            "Expression" = as.data.frame( expression ),
#            "Pooled" = pool.frame,
#            "Tail count" = as.data.frame( totals ),
#            "Mean tail length" = as.data.frame( means ),
#            "StdDev tail length" = as.data.frame( sds )
#        ),
#        sprintf("%s-statistics.csv", prefix),
#        comments = c(
#            '',
#            'Expression columns are quantile normalized log2 Reads Per Million as produced by the limma voom function',
#            ''
#        )
#    )
#    
#    """



@config.help(
"""\
Show a heatmap of the number of reads with different tail lengths in different genes.
""",
"""\
- Reads with an estimated tail length of 3 or less are ignored.

- Reads are pooled over all samples.
"""
)
@config.Positional('aggregate', 'Prefix of output from "aggregate-tail-lengths:"')
@config.Int_flag('min_tails', 'Minimum number of reads with tails in order to include in heatmap.')
@config.Float_flag('min_svd', 'Attempt to pick a sample of genes representative of all the different types of variation. Zero = no filter.')
@config.Int_flag('top', 'Furthermore, only include the top n features by total reads. Zero = include all.')
class Plot_pooled(config.Action_with_prefix, runr.R_action):
    aggregate = None
    top = 0
    min_tails = 1000
    min_svd = 0

    script = r"""
    library(nesoni)
    
    # === Load data ===
        
    annotation <- read.grouped.table(sprintf('%s/counts.csv',aggregate),require='Annotation')$Annotation
    pooled <- as.matrix( read.grouped.table(sprintf('%s/pooled.csv',aggregate))$All )
    
    maxtail <- ncol(pooled)
    
    
    # === Normalize by row maximum ===

    # Omit columns for tail length 0,1,2,3    
    skip <- 4

    norm.pooled <- pooled[,(skip+1):maxtail,drop=FALSE]
    for(i in basic.seq(ncol(norm.pooled)))
        if (i%%10 != 1)
            colnames(norm.pooled)[i] <- ''
    
    col.length <- basic.seq(ncol(norm.pooled)) + (skip-1)

    pool.total <- rep(0,nrow(pooled))
    pool.mean <- rep(0,nrow(pooled))
    pool.sd <- rep(0,nrow(pooled))        

    for(i in basic.seq(nrow(pooled))) {
        pool.total[i] <- sum(norm.pooled[i,])

        pool.mean[i] <- sum(norm.pooled[i,] * col.length) / pool.total[i]
        
        if (pool.total[i] > 1) {
            deviation <- col.length - pool.mean[i]
            pool.sd[i] <- sqrt( sum( deviation*deviation*norm.pooled[i,] ) / (pool.total[i]-1) )
        }
        
        norm.pooled[i,] <- norm.pooled[i,] / max(norm.pooled[i,],1)
    }


    # === Pick interesting genes ===
        
    keep <- (pool.total > min_tails)
    
    if (min_svd > 0) {
        keep[keep] <- svd.gene.picker( scale(norm.pooled[keep,,drop=FALSE]), min.svd=min_svd )
    }
    
    if (top > 0) {
        keep[keep] = ( rank(pool.total[keep]) >= (sum(keep)-top) )
    }
    
    kept.annotation <- annotation[keep,,drop=FALSE]    
    kept.norm.pooled <- norm.pooled[keep,,drop=FALSE]
    kept.pool.total <- pool.total[keep]
    kept.pool.mean <- pool.mean[keep]
    kept.pool.sd <- pool.sd[keep]

    # === Output ===

    n.rows <- nrow(kept.norm.pooled)
    row.labels <- n.rows <= 300        
    height <- if(row.labels) (25*n.rows+600) else 2500        
    png(sprintf("%s.png",prefix), width=2000, height=height, res=150)
    
    if (row.labels) {
       labels <- list(
           kept.annotation[,'gene'],
           rownames(kept.annotation), 
           kept.annotation[,'product']
       )
    } else {
       labels <- NULL
    }
    
    result <- nesoni.heatmap( 
        kept.norm.pooled, labels, 
        sort.mat=kept.norm.pooled, signed=FALSE, legend='number of reads\nscaled by row maximum')

    dev.off()


    # === Output CSV ===

    shuffle <- rev(result$dend.row$order)

    sink(sprintf('%s.csv', prefix))
    cat('# Pooled tail-length heatmap data\n')
    cat('#\n')
    cat('# Reads are counted as having a tail if there is a poly-A sequence of length 4 or greater\n')
    cat('#\n')

    frame <- data.frame( 
        Name = rownames(kept.annotation)[shuffle],
        Gene = kept.annotation$gene[shuffle],
        Product = kept.annotation$product[shuffle],
        'Cluster hierarchy' = rev(result$dend.row$paths),
        'Total reads with tails' = kept.pool.total[shuffle],
        'Mean tail length' = kept.pool.mean[shuffle],
        'Standard deviation tail length' = kept.pool.sd[shuffle],
        check.names=FALSE
    )

    write.csv(frame, row.names=FALSE)
    sink()

    """

@config.help(
'Produce a heatmap comparing average tail lengths between samples for different genes.'
)
@config.Positional('aggregate', 'Prefix of output from "aggregate-tail-lengths:"')
@config.Int_flag('min_tails', 'Minimum number of reads with tails *in each sample* required in order to include a gene.')
@config.Float_flag('min_span', 'Minimum difference in average tail lengths required in order to include a gene.')
@config.String_flag('vst_method')
class Plot_comparison(config.Action_with_prefix, runr.R_action):
    aggregate = None
    min_tails = 100
    min_span = 4.0
    vst_method = "anscombe.nb"

    script = r"""
    library(nesoni)
    
    # === Load data ==
    
    #data <- read.grouped.table(sprintf('%s/counts.csv',aggregate),require=c('Count','Annotation'))
    #annotation <- data$Annotation
    #expression <- voom( as.matrix(data$Count), normalize.method='quantile' )$E
    
    dgelist <- read.counts(sprintf('%s/counts.csv',aggregate))
    expression <- vst.rpm.counts(dgelist, vst_method)$E
    annotation <- dgelist$genes
    
    raw <- as.matrix(read.grouped.table(sprintf('%s/raw.csv',aggregate))$All)
    
    cols <- as.matrix(read.grouped.table(sprintf('%s/raw-columns.csv',aggregate))$All)   
    
    n.samples <- nrow(cols)
    n.genes <- nrow(raw)
    n.lengths <- ncol(cols)
    lengths <- basic.seq(n.lengths)-1


    # === Calculate mean tail lengths for each sample ===
    
    # Omit columns for tail length 0,1,2,3    
    skip <- 4
    good.cols <- cols[,(skip+1):n.lengths,drop=FALSE]
    good.lengths <- lengths[(skip+1):n.lengths]
    
    totals <- matrix(nrow=n.genes,ncol=n.samples) 
    means <- matrix(nrow=n.genes,ncol=n.samples)
    colnames(means) <- rownames(cols)
    for(c in basic.seq(n.samples)) {
        totals[,c] <- rowSums(raw[,good.cols[c,],drop=FALSE])
        means[,c] <- rowSums( t(t(raw[,good.cols[c,],drop=FALSE]) * good.lengths) ) / totals[,c]
        means[totals[,c] < min_tails,c] <- NA
    }


    # === Choose interesting genes to keep ===
    
    keep <- (
        row.apply(totals,min) >= min_tails &
        row.apply(means,min) <= row.apply(means,max) - min_span
    )
    
    kept.annotation <- annotation[keep,,drop=FALSE]
    kept.raw <- raw[keep,,drop=FALSE]
    kept.totals <- totals[keep,,drop=FALSE]    
    kept.means <- means[keep,,drop=FALSE]
    kept.expression <- expression[keep,,drop=FALSE]

    # === Cluster and travelling-salesman by tail length ===
        
    dend.row <- do.dendrogram( t(scale(t(kept.means))) )

       
    # === Output heatmap ===
    
    n.rows <- sum(keep)
    row.labels <- n.rows <= 300        
    height <- if(row.labels) (25*n.rows+700) else 2500        
    png(sprintf("%s.png",prefix), width=2000, height=height, res=150)

    if (row.labels) {
       labels <- list(
           kept.annotation[dend.row$order,'gene'],
           rownames(kept.annotation)[dend.row$order], 
           kept.annotation[dend.row$order,'product']
       )
    } else {
       labels <- NULL
    }
    
    plots <- list(
        list(
            weight=1,
            type='dendrogram',
            data=dend.row
        ),
        list(
            weight=1,
            type='heatmap',
            data=t(scale(t(kept.means[dend.row$order,,drop=FALSE]),scale=FALSE,center=TRUE)),
            signed=TRUE,
            legend='Tail length\n vs row average',
            title='Tail length  -vs-'
        ),
        list(
            weight=0.3,
            type='scatter',
            data=rowMeans( kept.means[dend.row$order,,drop=FALSE] ),
            title='row\naverage'
        ),
        list(
            weight=1,
            type='heatmap',
            data=t(scale(t( kept.expression[dend.row$order,,drop=FALSE] ),scale=FALSE,center=TRUE)),
            signed=TRUE,
            legend='log2 Reads Per Million\nvs row average',
            title='log2 RPM  -vs-'
        ),
        list(
            weight=0.3,
            type='scatter',
            data=rowMeans( kept.expression[dend.row$order,,drop=FALSE] ),
            title='row\naverage'
        )
    )
    
    multiplot(
        plots,
        labels
    )
    
    dev.off()


    # === Output CSV ===

    shuffle <- rev(dend.row$order)

    sink(sprintf('%s.csv', prefix))
    cat('# Tail-length heatmap data\n')
    cat('#\n')
    
    frame <- data.frame( 
        Name = rownames(kept.annotation)[shuffle],
        Gene = kept.annotation$gene[shuffle],
        Product = kept.annotation$product[shuffle],
        'Cluster hierarchy' = rev(dend.row$paths),
        check.names=FALSE
    )
    
    for(i in basic.seq(n.samples))
        frame[, paste(colnames(kept.means)[i],'mean tail')] <- kept.means[shuffle,i,drop=FALSE]

    for(i in basic.seq(n.samples))
        frame[, paste(colnames(kept.means)[i],'log2 count')] <- kept.expression[shuffle,i,drop=FALSE]

    write.csv(frame, row.names=FALSE)
    sink()
        
    """



@config.help(
    'Merge counts from groups of samples (eg replicates) into one.',
    'Counts are added, and tail lengths and proportions averaged.'
    )
@config.Positional('counts', '...-counts.csv file produced by "aggregate-tail-lengths:".')
@config.Main_section('groups', 'New samples, given as a list of <selection>=<name>.')
class Collapse_counts(config.Action_with_prefix):
    counts = None
    groups = [ ]
    
    def run(self):
        data = io.read_grouped_table(
            self.counts,
            [('Count',str), ('Annotation',str), ('Tail_count',str), ('Tail',str), ('Proportion',str)],
            'Count',
            )
        
        features = data['Count'].keys()
        samples = data['Count'].value_type().keys()
        
        tags = { }
        for sample in samples:
            tags[sample] = [sample]        
        for line in data.comments:
            if line.startswith('#sampleTags='):
                parts = line[len('#sampleTags='):].split(',')
                tags[parts[0]] = parts
        
        group_names = [ ]
        groups = [ ]
        group_tags = [ ]
        
        for item in self.groups:
            select = selection.term_specification(item)
            name = selection.term_name(item)
            group = [ item for item in samples if selection.matches(select, tags[item]) ]
            assert group, 'Empty group: '+name
            
            this_group_tags = [ name ]
            for tag in tags[group[0]]:
                if tag == name: continue
                for item in group[1:]:
                    for item2 in tags[item]:
                        if tag not in item2: break
                    else:
                        this_group_tags.append(tag)
            
            group_names.append(name)
            groups.append(group)
            group_tags.append(this_group_tags)
        
        result = io.Grouped_table()
        result.comments = [ '#Counts' ]
        for item in group_tags:
            result.comments.append('#sampleTags='+','.join(item))
        
        
        count = [ ]
        tail_count = [ ]
        tail = [ ]
        proportion = [ ]
        for feature in features:
            this_count = [ ]
            this_tail_count = [ ]
            this_tail = [ ]
            this_proportion = [ ]
            for group in groups:
                this_this_count = [ ]
                this_this_tail_count = [ ]
                this_this_tail = [ ]
                this_this_proportion = [ ]
                for sample in group:
                    this_this_count.append(int(data['Count'][feature][sample]))
                    this_this_tail_count.append(int(data['Tail_count'][feature][sample]))
                    item = data['Tail'][feature][sample]
                    if item != 'NA': this_this_tail.append(float(item))
                    item = data['Proportion'][feature][sample]
                    if item != 'NA': this_this_proportion.append(float(item))
                
                this_count.append(str(sum(this_this_count)))
                this_tail_count.append(str(sum(this_this_tail_count)))
                this_tail.append(str(sum(this_this_tail)/len(this_this_tail)) if this_this_tail else 'NA')
                this_proportion.append(str(sum(this_this_proportion)/len(this_this_proportion)) if this_this_proportion else 'NA')
                    
            count.append(this_count)
            tail_count.append(this_tail_count)
            tail.append(this_tail)
            proportion.append(this_proportion)
        
        matrix = io.named_matrix_type(features,group_names)
        result['Count'] = matrix(count)
        result['Annotation'] = data['Annotation']
        result['Tail_count'] = matrix(tail_count)
        result['Tail'] = matrix(tail)
        result['Proportion'] = matrix(proportion)
        result.write_csv(self.prefix + '.csv')





@config.help(
'Analyse expression levels and tail lengths of a set of samples.'
"""\
"""
)
@config.String_flag('annotations', 'Filename containing annotations. Defaults to annotations in reference directory.')
@config.String_flag('types', 'Comma separated list of feature types to use.')
@config.String_flag('parts', 'Comma separated list of feature types that make up features. Defaults to types if blank.')
@config.String_flag('spike_in', 'Comma separated list of spike-in "genes".')
@config.Int_flag('extension', 'How far downstrand of the given annotations a read or peak belonging to a gene might be.')
@config.Int_flag('tail',
     'Minimum tail length to count as having a tail.'
     )
@config.Int_flag('adaptor',
     'Minimum number of adaptor bases required, 0 for no filtering.'
     )
@config.Int_flag('saturation',
     'Duplicate start position saturation level. '
     'Reads that start at the same position will only '
     'count as up to this many. Zero for no saturation.'
     )
@config.String_flag('title', 'Report title.')
@config.String_flag('file_prefix', 'Prefix for filenames in report.')
@config.String_flag('reuse')
@config.Main_section('working_dirs', 'Sample directories, or full pipeline output directories')
class Analyse_tail_counts(config.Action_with_output_dir):         
    annotations = None
    types = 'gene'
    parts = 'exon'
    spike_in = ''
    extension = None
    tail = 4
    adaptor = 0
    saturation = 0
    title = 'PAT-Seq expression analysis'
    file_prefix = ''
    working_dirs = [ ]
    
    reuse = ''

    #def log_filename(self):
    #    if self.prefix is None: return None
    #    return self.prefix + '_analyse_tail_lengths_log.txt'
        
    def run(self):
        assert self.extension is not None, '--extension must be specified'
        
        # Also allow simply the analyse-polya-batch directory
        working_dirs = [ ]        
        for item in self.working_dirs:
            state_filename = os.path.join(item,'analyse-polya-batch.state')
            if not os.path.exists(state_filename):
                working_dirs.append(item)
            else:
                with open(state_filename,'rb') as f:
                    state = pickle.load(f)

                for sample in state.samples:
                    working_dirs.append(os.path.join(item,'samples',sample.output_dir))

        work = self.get_workspace()
        
        if self.reuse:
            pickle_workspace = workspace.Workspace(os.path.join(self.reuse,'pickles'))
        else:
            pickle_workspace = workspace.Workspace(work/'pickles')
        plot_workspace = workspace.Workspace(work/'plots')
        
        pickle_filenames = [ ]
        
        file_prefix = self.file_prefix
        if file_prefix and not file_prefix.endswith('-'):
            file_prefix += '-'

        with nesoni.Stage() as stage:
            for dir in working_dirs:
                working = working_directory.Working(dir, must_exist=True)
                pickle_filenames.append(pickle_workspace/working.name+'.pickle.gz')
                if self.reuse: continue
                Tail_count(
                    pickle_workspace/working.name,
                    working_dir=dir, 
                    annotations=self.annotations,
                    types=self.types,
                    parts=self.parts,
                    extension=self.extension,
                    ).process_make(stage)    
        
        assert len(set(pickle_filenames)) == len(pickle_filenames), "Duplicate sample name."
        
        with nesoni.Stage() as stage:
            Aggregate_tail_counts(
                output_dir=self.output_dir, 
                pickles=pickle_filenames,
                saturation=self.saturation,
                tail=self.tail,
                adaptor=self.adaptor
                ).process_make(stage)
        
        nesoni.Norm_from_counts(
            prefix=work/'norm',
            counts_filename=work/'counts.csv',
            ).make() 


        #vst = nesoni.Vst(
        #    prefix=work/'mlog',
        #    counts=work/'counts.csv',
        #    norm_file=work/'norm.csv',
        #    )
            
        similarity = nesoni.Similarity(
            prefix=plot_workspace/'similarity',
            counts=work/'counts.csv',
            )
        
        plot_pooleds = [        
            Plot_pooled(
                prefix = plot_workspace/'pooled-heatmap',
                aggregate = self.output_dir,
                #min_tails = min_tails,
                min_tails = 1,
                top = 100,
                )
            #for min_tails in (20,50,100,200,500,1000,2000)
            ]

        plot_comparisons = [
            Plot_comparison(
                prefix = plot_workspace/('comparison-min-tails-%d-min-span-%.1f' % (min_tails,min_span)),
                aggregate = self.output_dir,
                min_tails = min_tails,
                min_span = min_span,
                )
            for min_tails in [50,100,200,500]
            for min_span in [2,4,8,10,15,20,25,30]
            ]

        heatmaps = [
            nesoni.Heatmap(
                prefix = plot_workspace/('heatmap-min-fold-%.1f' % fold),
                counts = work/'counts.csv',
                norm_file = work/'norm.csv',
                min_span = math.log(fold)/math.log(2.0),
                )            
            for fold in [ 1.5, 2.0, 4.0, 6.0, 8.0, 10.0, 20.0, 30.0, 40.0 ]
            ]

        with nesoni.Stage() as stage:        
            #vst.process_make(stage)
            similarity.process_make(stage)
            for action in plot_pooleds + plot_comparisons + heatmaps:
                action.process_make(stage)
        
        
        r = reporting.Reporter(work/'report', 
            self.title,
            file_prefix,
            )         
        
        saturation = self.saturation
        if saturation:
            r.p(
                'Duplicate read removal: Sets of reads aligning with exactly the start and end position in the reference were counted as a single read.'
            )

        #r.p(
        #    'This scatterplot show the number of reads aligning to each gene between each pair of samples. '
        #    'This can be used to discover poor samples.'
        #)        
        #
        #r.p( r.get(self.prefix + '-count.png', image=True) )
        
        
        
        
        #r.heading('Spreadsheet with statistics for all genes and all samples')
        #
        #if saturation:
        #    r.p( 'Note: Reads with the same start and end position sometimes don\'t have the same tail length. '
        #         'After deduplication these can contribute fractionally to the number of reads with tails.' )
        #
        ##r.p( r.get(self.prefix + '-statistics.csv') )
        #r.p( r.get(self.prefix + '-counts.csv') )
        
        similarity.report(r)
        
        r.heading('Poly(A) tail length distribution')
                
        r.p(
            'This plot shows the distribution of lengths of poly(A) tail sequence in top expressed features. '
            'Its main purpose is to assess data quality. '
            'If the plot has many bright spots there may be many identical reads, possibly due to non-random digestion.'
            )
        
        r.p(
            'Only reads with a poly(A) sequence of four or more bases are used.'
            )
        
        for heatmap in plot_pooleds:
            r.report_heatmap(heatmap)
            
        r.heading('Heatmaps')
        
        r.p(
            'Genes were selected based '
            'on there being at least some fold change difference between '
            'some pair of samples.'
        )
        
        #r.p(
        #    'The log2 counts have been quantile normalized.'
        #)
        
        for heatmap in heatmaps:
            r.report_heatmap(heatmap)


        r.heading('Average poly(A) tail length and its relation to expression levels')
        
        r.p(
            'Only reads with a poly(A) sequence of four or more bases was included in the averages.'
            )
        
        r.p(
            'Genes were selected based on there being at least a certain number of reads with poly(A) sequence in <i>each</i> sample (min-tails), '
            'and on there being at least some amount of difference in average tail length between samples (min-span).'
            )
        
        for heatmap in plot_comparisons:
            r.report_heatmap(heatmap)
        

        #r.heading('Raw data')
        #
        #r.p(r.get(work/'counts.csv') + ' - raw counts and tail statistics')
        #r.p(r.get(work/'glog.csv') + ' - glog2 RPM counts')
        #r.p(r.get(work/'norm.csv') + ' - normalization factors used')
        #
        #r.write('<p/><hr>\n')
        #r.subheading('About normalization and log transformation')
        #
        #r.p('Counts are converted to '
        #    'log2 Reads Per Million using a variance stabilised transformation. '
        #    'Let the generalised logarithm with moderation m be')
        #
        #r.p('glog(x,m) = log2((x+sqrt(x*x+4*m*m))/2)')
        #
        #r.p('then the transformed values will be')
        #
        #r.p('glog( count/library_size*1e6, log_moderation/mean_library_size*1e6 )')
        #
        #r.p('where log_moderation is a parameter, here 5. '
        #    'The library sizes used are effective library sizes after TMM normalization.')
        
        r.close()








