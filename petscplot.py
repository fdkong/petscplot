#! /usr/bin/env python3
# -*- mode: Python -*-
from __future__ import division
from __future__ import print_function
from builtins import map
from builtins import next
from builtins import range
from builtins import object
from numpy import *
from matplotlib.pyplot import *
import matplotlib.ticker as ticker
import matplotlib
import pprint, re, itertools, os.path, locale
from collections import namedtuple
from re import compile,MULTILINE
from funcparserlib.lexer import make_tokenizer,Token
from funcparserlib.contrib.common import sometok,unarg
from funcparserlib.parser import maybe, many, oneplus, finished, skip, forward_decl, NoParseError

#from IPython.Debugger import Tracer; debug_here = Tracer()

class SNES(object):
    def __init__(self, indent, reason, res, level=None):
        self.indent = indent
        self.reason = reason
        self.res = res
        self.level = None
        self._name = None
    def name(self, name=None):
        if name:
            self._name = name
        elif self.level:
            return 'Level %d (%s nodes)' % (self.level.level, locale.format("%d",self.level.count,grouping=True))
        else:
            return 'Unknown'
    def __repr__(self):
        return 'SNES(indent=%r, reason=%r, res=%r, level=%r)' % (self.indent, self.reason, self.res, self.level)

def Dict(**args): return args
KSP    = namedtuple('KSP', 'reason res')
SNESIt = namedtuple('SNESIt', 'indent res ksp')
Event  = namedtuple('Event', 'name count time flops Mflops')
Stage  = namedtuple('Stage', 'name events')

class Run(object):
    def __init__(self, levels, solves, exename, petsc_arch, hostname, np, wtime, stages, options, **args):
        self.levels     = levels
        self.solves     = solves
        self.exename    = exename
        self.petsc_arch = petsc_arch
        self.hostname   = hostname
        self.np         = np
        self.wtime      = wtime
        self.stages     = stages
        self.options    = options
        self.__dict__.update(args)
    def __repr__(self):
        return 'Run(%s)' % ', '.join('%s=%r' % (k,v) for (k,v) in list(self.__dict__.items()))

def span(pred):
    def go(lst):
        for i in range(len(lst)):
            if not pred(lst[i]):
                return lst[:i], lst[i:]
        return (lst,[])
    return go
def groupBy(f,lst):
    'Group input, f(elem) == None is agnostic'
    chunks = []
    next = lst
    while next:
        cur = f(next[0])
        a, next = span(lambda x: (f(x) is None) or (f(x) == cur))(next)
        chunks.append(a)
    return chunks

class Level(object):
    def __init__(self,level,Lx,Ly,Lz,M,N,P,count,hx,hy,hz):
        scope = locals() # Bind the free variables in this scope
        def mkprops(cvt,props):
            for p in props.split():
                setattr(self,p,cvt(scope[p]))
        mkprops(float,'Lx Ly Lz hx hy hz')
        mkprops(int,'level M N P count')
        # Input validation
        assert(self.M*self.N*self.P == self.count)
        fuzzy_equals = lambda a,b: abs(a-b) / (abs(a)+abs(b)) < 1e-3
        for (l,m,h) in [s.split() for s in ['Lx M hx','Ly N hy']]:
            assert(fuzzy_equals(getattr(self,l)/getattr(self,m), getattr(self,h)))
        if (P > 1):
            assert(fuzzy_equals(self.Lz/(self.P-1), self.hz))
    def __repr__(self):
        return ('Level(level=%r, Lx=%r, Ly=%r, Lz=%r, M=%r, N=%r, P=%r, count=%r, hx=%r, hy=%r, hz=%r)'
                % tuple(getattr(self,p) for p in 'level Lx Ly Lz M N P count hx hy hz'.split()))

def tokenize(str):
    'str -> Sequence(Token)'
    specs = [
        (u'level', (r'^Level \d+.*$',MULTILINE)),
        (u'snes_monitor', (r'^\s+\d+ SNES Function norm.*$',MULTILINE)),
        (u'snes_converged', (r'^\s*Nonlinear solve converged due to \w+$',MULTILINE)),
        (u'snes_diverged', (r'^\s*Nonlinear solve did not converge due to \w+$',MULTILINE)),
        (u'ksp_monitor', (r'^\s+\d+ KSP Residual norm.*$',MULTILINE)),
        (u'ksp_converged', (r'^\s*Linear solve converged due to \w+$',MULTILINE)),
        (u'ksp_diverged', (r'^\s*Linear solve did not converge due to \w+$',MULTILINE)),
        (u'max_wall_time',(r'^Time \(sec\):\s+\d\.\d{3}e[-+]\d\d\s+\d\.\d{5}\s+\d\.\d{3}e[-+]\d\d$',MULTILINE)),
        (u'event', (r'^\S{1,16}\s+\d+ \d\.\d \d\.\d{4}e[-+]\d\d \d\.\d \d\.\d\de[-+]\d\d \d\.\d (\d\.\de[-+]\d\d ){2}.*$',MULTILINE)),
        (u'stage', (r'^--- Event Stage \d+: .*$',MULTILINE)),
        (u'memory_usage', (r'^Memory usage is given in bytes:',MULTILINE)),
        (u'summary_begin', (r'^---------------------------------------------- PETSc Performance Summary: ----------------------------------------------$',MULTILINE)),
        (u'hostline', (r'^\S+ on a \S+ named \S+ with \d+ processors?, by .*$',MULTILINE)),
        (u'option_table_begin', (r'^#PETSc Option Table entries:$',MULTILINE)),
        (u'option_table_entry', (r'^-\w+(\s+\w+)?$',MULTILINE)),
        (u'option_table_end', (r'^#End of? PETSc Option Table entries$',MULTILINE)),
        (u'nl', (r'[\r\n]+',)),
        (u'other', (r'^.*$',MULTILINE)), # Catches all lines that we don't understand
    ]
    ignored = 'nl other'.split()
    t = make_tokenizer(specs)
    return [x for x in t(str) if x.type not in ignored]

def parse(seq):
    'Sequence(Token) -> object'
    Host = namedtuple('Host', 'exename arch host np')
    LogSummary = namedtuple('LogSummary', 'host wtime stages options')
    def mkLevel(s):
        rfloat = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
        rint   = r'[-+]?\d+'
        capture = lambda m: '('+m+')'
        within_space = lambda m: r'\s*'+m+r'\s*'
        cfloat, cint = [within_space(capture(m)) for m in [rfloat,rint]]
        x = within_space('x')
        m = re.match('Level'+cint+r'domain size \(m\)'+cfloat+x+cfloat+x+cfloat
                     +', num elements'+cint+x+cint+x+cint+r'\('+cint
                     +r'\), size \(m\)'+cfloat+x+cfloat+x+cfloat, s)
        return Level(*m.groups())
    def mkSNESIt(s):
        resline, ksp = s[0], s[1]
        res = float(resline.strip().split()[4])
        indent = len(re.match(r'^( *)(?:  | \d|\d\d)\d', resline).groups()[0]) // 2
        return SNESIt(indent,res,ksp)
    def mkKSPIt(s):
        return float(s.strip().split()[4])
    def mkKSP(s):
        return KSP(reason=('UNKNOWN' if len(s)==1 else s[1]), res=s[0])
    def mkSNES(s):
        res = s[0]
        reason = s[1]
        indent = res[0].indent
        for it in res[1:]:
            if it.indent != indent:
                raise RuntimeError('SNES monitors changed levels, perhaps -snes_converged_reason is missing:\n\tstarted with: %s\n\tunexpected: %s' %(res[0],it))
        return SNES(level=None, indent=indent, reason=s[1], res=s[0])
    def mkEvent(s):
        s = s.split()
        return Event(name=s[0], count=int(s[1]), time=float(s[3]), flops=float(s[5]), Mflops=float(s[-1]))
    def mkStage(stageheader, events):
        name = re.match(r'^--- Event Stage \d+: (.*)', stageheader).groups()[0]
        eventdict = dict((e.name,e) for e in events)
        return Stage(name, eventdict)
    def mkOption(s):
        return re.match(r'^(-\w+)(?:\s+(.+))?$',s).groups()
    def mkRun(levels, solves, logs):
        for x in solves:
            x.level = levels[-1-x.indent]
        if not logs:
            logs = LogSummary(host=Host('unknown', 'unknown', 'unknown', -1), wtime=[], stages=[], options=[])
        exename = logs[0].host.exename
        arch = logs[0].host.arch
        host = logs[0].host.host
        np = logs[0].host.np
        wtime = logs[0].wtime
        stages = logs[0].stages
        options = logs[0].options
        if len(logs) > 1:
            meanwtime = sum(h.wtime for h in logs)/len(logs)
            rej = 0.15
            logs = [h for h in logs if h.wtime<(1+rej)*meanwtime and h.wtime>(1-rej)*meanwtime] # Exclude outliers
            nlogs = len(logs)
            wtime = sum(h.wtime for h in logs)/nlogs
            for i,stage in enumerate(stages):
                for event in stage.events:
                    stage.events[event]._replace(time = sum([h.stages[i].events[event].time for h in logs])/nlogs)
                    stage.events[event]._replace(flops = sum([h.stages[i].events[event].flops for h in logs])/nlogs)
                    stage.events[event]._replace(Mflops = sum([h.stages[i].events[event].Mflops for h in logs])/nlogs)
        return Run(levels, solves, exename, arch, host, np, wtime, stages, options)
    def mkHost(s):
        (exename, arch, host, np) = re.match(r'^(\S+) on a (\S+) named (\S+) with (\d+) processors?, by .*$', s).groups()
        return Host(exename, arch, host, int(np))
    def mkMWTime(s):
        mwtime = re.match(r'^Time \(sec\):\s+(\d\.\d{3}e[-+]\d\d)\s+\d\.\d{5}\s+\d\.\d{3}e[-+]\d\d$',s).groups()[0]
        return float(mwtime)

    level = sometok('level') >> mkLevel
    kspit = sometok('ksp_monitor')   >> mkKSPIt
    ksp_converged = sometok('ksp_converged') >> (lambda s: s.strip().split()[5])
    ksp_diverged = sometok('ksp_diverged') >> (lambda s: s.strip().split()[7])
    ksp   = many(kspit) + maybe(ksp_converged | ksp_diverged) >> mkKSP
    snesit = sometok('snes_monitor') + maybe(ksp) >> mkSNESIt
    snes_converged = sometok('snes_converged') >> (lambda s: s.strip().split()[5])
    snes_diverged = sometok('snes_diverged') >> (lambda s: s.strip().split()[7])
    snes  = oneplus(snesit) + (snes_converged | snes_diverged) >> mkSNES
    event = sometok('event') >> mkEvent
    stage = sometok('stage') + many(event) >> unarg(mkStage)
    memory_usage = sometok('memory_usage') + many(sometok('stage')) # No plans for memory usage
    option_table_entry = sometok('option_table_entry') >> mkOption
    option_table = skip(sometok('option_table_begin')) + many(option_table_entry) + skip(sometok('option_table_end')) >> dict
    host = sometok('hostline') >> mkHost
    max_wall_time = sometok('max_wall_time') >> mkMWTime
    log_summary = skip(sometok('summary_begin')) + host + max_wall_time + many(stage) + skip(memory_usage) + option_table >> unarg(LogSummary)
    petsc_log = many(level) + many(snes) + many(log_summary) + skip(finished) >> unarg(mkRun)
    return petsc_log.parse(seq)

def read_file(fname):
    with open(fname) as f:
        s = f.read()
    return s

def fit_loglog(x, y, flag='mean'):
    m,b = polyfit(log(x),log(y),1) # Linear fit
    if flag == 'mean':
        return exp(log(x)*m + b)
    else:
        raise RuntimeError('not implemented')

def fit_ideal_loglog(x,y,slope):
    xmin, ymin = log(x[0]), log(y[0])
    b = ymin - xmin*slope;
    ymax = log(y[-1])
    xmax = (ymax - b)/slope     # Find the x associated with the rightmost data point
    fit_x = log(x) #array([xmin, xmax])
    fit_y = fit_x*slope + b
    return (exp(fit_x), exp(fit_y))

def plot_loglog(x,y,color,marker,name,loc):
    rm,rb = polyfit(log(x),log(y),1)
    plot(x,exp(log(x)*rm+rb),color,linewidth=1)
    loglog(x,y,color[0]+marker,label='%s slope=%5.3f'%(name,rm),markersize=12)
    #text(3.5e6,loc,'slope=%5.3f'%rm)
    #text(3.5e6,loc,'slope=%5.3f'%rm)

def get_dims(args, dflt_width, ratio=(sqrt(5)-1)/2):
    inches_per_pt = 1/72.27
    width = inches_per_pt * (args.width_pt if args.width_pt else dflt_width)
    height = width * ratio
    return width, height

def set_sizes_talk(opts):
    fig_size = get_dims(opts, 900)
    rcParams.update({'axes.titlesize': 18,
                     'axes.labelsize': 18,
                     'font.size': 24,
                     'legend.fontsize': 18,
                     #'legend.markerscale' : 8,
                     'xtick.labelsize': 18,
                     'ytick.labelsize': 18,
                     'text.usetex': True,
                     'figure.figsize': fig_size})
    subplots_adjust(left=0.08,right=0.975,bottom=0.08,top=0.94)

def set_sizes_poster(opts):
    fig_size = get_dims(opts, 1240)
    rcParams.update({'axes.titlesize': 24,
                     'axes.labelsize': 24,
                     'font.size': 32,
                     'lines.linewidth': 3,
                     'lines.markersize': 10,
                     'legend.fontsize': 24,
                     'xtick.labelsize': 24,
                     'ytick.labelsize': 24,
                     'text.usetex': True,
                     'figure.figsize': fig_size})
    subplots_adjust(left=0.09,right=0.975,bottom=0.11,top=0.93)

def set_sizes_paper(opts):
    fig_size = get_dims(opts, 324)
    rcParams.update({'axes.titlesize': 11,
                     'axes.labelsize': 11,
                     'font.size': 16,
                     'lines.linewidth': 1,
                     'lines.markersize':7,
                     'legend.fontsize': 9,
                     #'legend.markerscale' : 8,
                     'xtick.labelsize': 9,
                     'ytick.labelsize': 9,
                     'text.usetex': True,
                     'figure.figsize': fig_size})
    if opts.legend_outside :
      subplots_adjust(left=0.09,right=0.975,bottom=0.11,top=0.93)
    else :
      subplots_adjust(left=0.12,right=0.975,bottom=0.15,top=0.93)

def plot_snes_convergence(solves, sequence=False, withksp=False, legend_loc='lower left'):
    marker = itertools.cycle(list('osv^<>*D'))
    offset = 0
    for s in solves:
        res = array([[i,x.res] for (i,x) in enumerate(s.res)])
        name = s.name()
        semilogy(offset+res[:,0],res[:,1],'-'+next(marker),label=name)
        if withksp:
            for (k,ksp) in enumerate([r.ksp for r in s.res]):
                if not ksp.res:
                    continue # Skip when there are no linear iterations (SNES converged)
                kres = array(list(enumerate(ksp.res)))
                x = offset+k+kres[:,0]/(kres.shape[0]-1)
                semilogy(x,kres[:,1],'k:x',label=None)
        if sequence: offset += res.shape[0]-1
    ylabel('Residual')
    xlabel('Newton iteration')
    legend(loc=legend_loc)

def plot_snes(opts, logfiles):
    '''Plots the nonlinear convergence for a single file.'''
    if len(logfiles) != 1:
        raise RuntimeError('Must supply exactly one file for SNES plotting')
    run = parse(tokenize(read_file(logfiles[0])))
    legend_loc = opts.legend_loc if opts.legend_loc else 'lower left'
    solves = run.solves         # Have to bind this locally so that eval with work
    plot_snes_convergence(eval('solves[%s]' % opts.solve_spec), sequence=True, withksp=True, legend_loc=legend_loc)

def segment(logfiles):
    '''turn a flat colon-delimited list
           ['a', 'b', ':', 'c', 'd', 'e']
       into a generator of lists
           [['a', 'b'], ['c', 'd', 'e']]'''
    return [x for x in groupBy(lambda x: x==':', logfiles) if x != [':']]

def name_segments(opts,logfiles):
    if opts.legend_labels:
        return iter(opts.legend_labels)
    elif opts.series_labels:
        return iter(opts.series_labels)
    elif opts.series_labels == []:
        return iter(('\\verb|%s|' % l[0]) for l in segment(logfiles))
    else:
        return iter('' for l in segment(logfiles))

def plot_algorithmic(opts, logfiles):
    '''Plots algorithmic scalability for several file series.  File
    series are separated by ':' (must be escaped from the shell).  Each
    file series is expected to be a list of log files with common
    algorithm and increasing problem size.  The result is a log-log
    plot of iteration count for each series.
    '''
    marker = itertools.cycle(list('osv^<>*D'))
    plotter = loglog
    name = name_segments(opts,logfiles)
    for logs in segment(logfiles):
        series = [parse(tokenize(read_file(fname))) for fname in logs]
        if len(series) > 1:
            # Use the last solve in each run, presumably the finest grid used in grid sequencing, or last time step, etc.
            solves = [s.solves[-1] for s in series]
        else:
            solves = series[0].solves
        its = array([(s.level.count, mean([len(r.ksp.res) for r in s.res][:-1])) for s in solves])
        plotter(its[:,0],its[:,1],'-'+next(marker),label=next(name))
    ylabel('Krylov its. per nonlinear (rtol $10^{-2}$)')
    xlabel('Number of nodes')
    legend(loc=(opts.legend_loc if opts.legend_loc else 'upper left'))

def plot_strong(opts, logfiles):
    '''Plots strong scalability given a list of log files.  The file
    sequence should have a constant global problem size with increasing
    numbers of processes The result is a log-log plot showing time to
    solve the problem with successively more processes.'''
    marker = itertools.cycle(list('osv^<>*D'))
    (plotstage,) = opts.stages
    (plotevent,) = opts.events

    name = name_segments(opts,logfiles)
    allx = array([])
    ally = array([])
    for logs in segment(logfiles):
        series = [parse(tokenize(read_file(fname))) for fname in logs]
        series.sort(key=lambda a : a.np) # Sort files in increasing order of number of processes
        #solves = [s.solves[-1] for s in series]
        np = array([s.np for s in series])
        time = array([r.stages[plotstage].events[plotevent].time for r in series])
        allx, ally = concatenate((allx,np)), concatenate((ally,time))
        ideal_np, ideal_time = fit_ideal_loglog(np, time, slope=-1)
        if opts.efficiency:
            eff = ideal_time / time
            semilogx(np,eff,'-'+next(marker),label=next(name))
        else:
            line = loglog(np,time,'-'+next(marker),label=next(name))
            loglog(ideal_np,ideal_time,line[0].get_color(),label='ideal')
    xlim(min(allx)*0.7 if opts.xmin is None else opts.xmin,
         max(allx)*1.1 if opts.xmax is None else opts.xmax)
    xlabel('Number of processes')
    if opts.efficiency:
        ylim(0,1.2)
        ylabel('Parallel efficiency')
    else:
        ylim(min(ally)*0.8, max(ally)*1.1)
        ylabel('Time (seconds)')
    legend((opts.legend_loc if opts.legend_loc else 'upper right'), frameon=False)
    if opts.title is None:
        title('Strong scaling (\\texttt{%s})' % ', '.join(opts.events))
    elif opts.title:
        title(opts.title)

def plot_weak(opts, logfiles):
    def event_label(seriesname,event):
        if opts.legend_labels:
            return opts.legend_labels.pop(0)
        else:
            return '%s \\texttt{%s}'%(seriesname,event)
    marker = itertools.cycle(list('osv^<>*D'))
    (plotstage,) = opts.stages
    plotevents = opts.events
    names = list(name_segments(opts,logfiles))
    barwidth = 0.2
    colors = 'rgbmcyk'
    allx = array([])
    ally = array([])
    for i,logs in enumerate(segment(logfiles)):
        series = [parse(tokenize(read_file(fname))) for fname in logs]
        series.sort(key=lambda a : a.np) # Sort files in increasing order of number of processes
        np = array([s.np for s in series])
        lastline = None
        seriesname = names[i]
        bottom = zeros(len(np))
        xvalues = arange(len(np)) + i*barwidth
        for j,event in enumerate(plotevents):
            time = array([r.stages[plotstage].events[event].time for r in series])
            lastline = bar(xvalues, time, label=event_label(seriesname,event),
                           width=barwidth, color=colors[j], bottom=bottom)
            bottom += time
        ally = concatenate((ally,bottom))
        allx = concatenate((allx,np))
        xticks(xvalues+barwidth/2, list(map(str,np)))
    ylim(0, max(ally)*1.1)
    xlabel('Number of processes')
    ylabel('Time (seconds)')
    legend(loc=(opts.legend_loc if opts.legend_loc else 'upper left'))
    if opts.title is None:
        title('Weak scaling (\\texttt{%s})' % ', '.join(opts.events))
    elif opts.title:
        title(opts.title)

def plot_wtime(opts, logfiles):
    '''Plots strong scalability given a list of log files.  The file
    sequence should have a constant global problem size with increasing
    numbers of processes The result is a log-log plot showing time to
    solve the problem with successively more processes.'''
    marker = itertools.cycle(list('osv^<>*D'))

    name = name_segments(opts,logfiles)
    allx = array([])
    ally = array([])
    for logs in segment(logfiles):
        series = [parse(tokenize(read_file(fname))) for fname in logs]
        series.sort(key=lambda a : a.np) # Sort files in increasing order of number of processes
        #solves = [s.solves[-1] for s in series]
        np = array([s.np for s in series])
        time = array([r.wtime for r in series])
        allx, ally = concatenate((allx,np)), concatenate((ally,time))
        ideal_np, ideal_time = fit_ideal_loglog(np, time, slope=-1)
        if opts.efficiency:
            eff = ideal_time / time
            semilogx(np,eff,'-'+next(marker),label=next(name))
        else:
            line = loglog(np,time,'-'+next(marker),label=next(name))
            loglog(ideal_np,ideal_time,'--',color='gray',label='ideal')
    xlim(min(allx)*0.7 if opts.xmin is None else opts.xmin,
         max(allx)*1.1 if opts.xmax is None else opts.xmax)
    xlabel('Number of processes')
    xticks(np,np)
    if opts.efficiency:
        ylim(0,1.2)
        ylabel('Parallel efficiency')
    else:
        ylim(min(ally)*0.8, max(ally)*1.1)
        ylabel('Wall Time (seconds)')
    legend(numpoints=1,loc=(opts.legend_loc if opts.legend_loc else 'upper right'), frameon=False)
    if opts.title is None:
        title('Strong scaling (\\texttt{Wall time})')
    elif opts.title:
        title(opts.title)

def plot_flop(opts, logfiles):
    '''Plots strong scalability given a list of log files.  The file
    sequence should have a constant global problem size with increasing
    numbers of processes The result is a log-log plot showing time to
    solve the problem with successively more processes.'''
    ax = axes()
    marker = itertools.cycle(list('ov^<>*xd+H'))
    (plotstage,) = opts.stages
    plotevents = opts.events
    name = name_segments(opts,logfiles)
    colors = 'rgbmcyk'
    allx = array([])
    ally = array([])
    shift = 0
    for i,logs in enumerate(segment(logfiles)):
        series = [parse(tokenize(read_file(fname))) for fname in logs]
        series.sort(key=lambda a : a.np) # Sort files in increasing order of number of processes
        #solves = [s.solves[-1] for s in series]
        np = array([s.np for s in series])
        lastline = None
        for j,event in enumerate(plotevents):
          flop = array([s.stages[plotstage].events[event].Mflops/1024 for s in series])
          allx = concatenate((allx,np))
          ally = concatenate((ally,flop))
          shift += j*max(allx)*1.1
          line = ax.loglog(np+shift,flop,'-'+next(marker),label=next(name),alpha=0.3)
          #ideal_np,ideal_flop = fit_ideal_loglog(np,flop,slope=1)
          #loglog(ideal_np+shift,ideal_flop,'--',color='gray',label='ideal')
    ax.set_xlim(min(allx)*0.7 if opts.xmin is None else opts.xmin,
         max(allx)*1.1 if opts.xmax is None else opts.xmax)
    ax.set_xlabel('Number of processes')
    ax.set_ylabel('Performance [\\texttt{Gflop/s}]')
    ax.tick_params(axis='x',which='minor',bottom=False) # turn off minor ticks on x axis
    ax.xaxis.set_ticks(np)
    ax.xaxis.set_ticklabels(np)
    ax.yaxis.set_ticks([1,2,5,10,20,30,40,50])
    ax.yaxis.set_ticklabels([1,2,5,10,20,30,40,50])
    ax.set_ylim(min(ally)*0.8, max(ally)*2)
    ax.xaxis.set_minor_locator(ticker.NullLocator())
    ax.yaxis.set_minor_formatter(ticker.NullFormatter())
    if opts.legend_outside :
      # Shrink current axis by 40%
      box = ax.get_position()
      ax.set_position([box.x0, box.y0, box.width*0.67, box.height])
      ax.legend(numpoints=1,loc=(opts.legend_loc if opts.legend_loc else 'center left'), frameon=False,bbox_to_anchor=(1,0.5))
    else :
      ax.legend(numpoints=1,loc=(opts.legend_loc if opts.legend_loc else 'upper left'), frameon=False)

    if opts.title is None:
        ax.set_title('Strong scaling')
    elif opts.title:
        ax.set_title(opts.title)

def splitlist(sep, build):
    def constructor(string):
        return [build(x) for x in string.split(sep)]
    return constructor

def parse_options():
    try:
        from argparse import ArgumentParser
    except(ImportError):
        from optparse import OptionParser
        class ArgumentParser(OptionParser):
            def __init__(self,*args,**kwargs):
                self.pairs = dict()
                return OptionParser.__init__(self,*args,**kwargs)
            def add_argument(self, *args, **kwargs):
                if 'type' in kwargs:
                    thetype = kwargs['type']
                    if thetype == str:
                        kwargs['type'] = 'string'
                    elif thetype == int:
                        kwargs['type'] = 'int'
                    elif thetype == float:
                        kwargs['type'] = 'float'
                    elif thetype == bool:
                        kwargs['type'] = 'bool'
                    else:
                        def thecallback(option, opt, value, parser):
                            if value is not None:
                                setattr(parser.values,option.dest,thetype(value))
                        kwargs['type'] = 'string'
                        if 'default' in kwargs and kwargs['default'] is not None:
                            kwargs['default'] = thetype(kwargs['default'])
                        kwargs.update(dict(action='callback', callback=thecallback))
                if 'nargs' in kwargs: # Only supports one nargs option
                    self.nargskey = args[0]
                    kwargs['dest'] = self.nargskey
                    return
                self.pairs[kwargs['dest']] = kwargs.get('default',None)
                return OptionParser.add_option(self, *args, **kwargs)
            def parse_args(self):
                args, nargs = OptionParser.parse_args(self)
                setattr(args, self.nargskey, nargs)
                return args

    print(sys.argv)
    parser = ArgumentParser(description='Plot analytics on PETSc program output')
    parser.add_argument('-f', '--format', choices='native png pdf svg ps eps'.split(), help='Output format for plotting', dest='format')
    parser.add_argument('-m', '--mode', choices='talk poster paper'.split(), help='Formatting mode, affects weights and fonts', dest='mode', default='talk')
    parser.add_argument('-t', '--type', choices='snes algorithmic weak strong wtime flop'.split(), help='Plot type', dest='type', default='snes')
    parser.add_argument('-o', '--output', help='Output filename', dest='output')
    parser.add_argument('--legend-labels', help='Labels for each item being plotted, colon (:) separated', dest='legend_labels', type=splitlist(':',str))
    parser.add_argument('--legend-loc', help='Location of legend (e.g. "upper left")', dest='legend_loc')
    parser.add_argument('--legend-outside', help='put legend outside', dest='legend_outside')
    parser.add_argument('--series-labels', help='Labels for each series, legend labels are created from series labels', dest='series_labels', type=splitlist(':',str))
    parser.add_argument('--solve-spec', help='Which solves to use out of each model run (e.g. "1:")', dest='solve_spec', default='0:')
    parser.add_argument('--stages', help='Which stage to look at for timing numbers (colon-separated list)', dest='stages', type=splitlist(':',int), default='-1')
    parser.add_argument('--events', help='Which events to look at for timing (colon-separated list)', dest='events', type=splitlist(':',str), default='SNESSolve')
    parser.add_argument('--width-pt', help='Width, in LaTeX points, of the figure', dest='width_pt', type=float)
    parser.add_argument('--title', help='Set the figure title', dest='title', type=str)
    parser.add_argument('--efficiency', help='Plot parallel efficiency instead of a log-log representation', dest='efficiency', action='store_true')
    parser.add_argument('--xmin', dest='xmin', type=float)
    parser.add_argument('--xmax', dest='xmax', type=float)
    parser.add_argument('logfiles', nargs='+', type=str, help='List of files to process, usually with -log_summary')
    opts = parser.parse_args()

    {'talk' : set_sizes_talk, 'poster' : set_sizes_poster, 'paper' : set_sizes_paper}[opts.mode](opts)
    if opts.output and not opts.format:
        ext = os.path.splitext(opts.output)[1]
        supported = {'.png':'png', '.jpg':'jpg', '.eps':'eps', '.pdf':'pdf'}
        if ext in supported:
            opts.format = supported[ext]
        else:
            raise RuntimeError('Suffix "%s" not recognized, use a recognized extension, or set manually with --format' % ext)
    print('Plotting %s using format %s from files: %s' % (opts.type, opts.format, ' '.join(opts.logfiles)))
    if opts.format != 'native': rcParams.update({'backend': 'Agg'})
    return opts

if __name__ == "__main__":
    opts = parse_options()
    {'snes': plot_snes,
     'algorithmic' : plot_algorithmic,
     'strong' : plot_strong,
     'weak' : plot_weak,
     'wtime' : plot_wtime,
     'flop' : plot_flop,
     }[opts.type](opts, opts.logfiles)
    if opts.output:
        savefig(opts.output,bbox_inches="tight")
    else:
        show()
