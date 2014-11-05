# -*- coding: utf-8 -*-
"""
data - diffraction data access

This module defines a DataScan, a container to hold the experimental 
data of a ptychography scan. Instrument-specific reduction routines should
create an empty DataScan and store information in it. One should then call 
DataScan.save() to dump the file to disk in a uniform format, or pass
along the DataScan object directly to a Ptycho instance. Once saved, DataScan
also loads data.

For the moment the module contains two main objects:
DataScan, which holds a single ptychography scan, and DataSource, which
holds a collection of datascans and feeds the data as required.

TODO: Introduce another class that streams data as it is read.
TODO: Read/write using hdf5 MPI support, add support for cxi files.
TODO: Add possibility to not jump over bad data in DS.feed_data (PT ?)
TODO: Make names more uniform (e.g. scan_info -> meta) to avoid confusion.

This file is part of the PTYPY package.

    :copyright: Copyright 2014 by the PTYPY team, see AUTHORS.
    :license: GPLv2, see LICENSE for details.
"""
from threading import Thread, Event
import time
import os
import numpy as np
import copy

from ..utils import expect2, clean_path, mass_center, parallel
from ..utils.verbose import logger
from .. import io
from .. import utils as u

__all__=['DataScan', 'StaticDataSource','make_datasource']

DEFAULT_scan_info = u.Param(
    scan_number =  None, #Scan number
    scan_label =  'Scan%(idx)02d', #A string associated with the scan number
    data_filename =  None, #The file name the data file is going to be saved to
    wavelength =  None, #The radiation wavelength in meters
    energy =  None, #The photon energy 
    detector_pixel_size =  None, #Detector pixel dimensions in meters
    detector_distance =  None, #Distance between the detector and the sample in meters
    initial_ctr =  None, #The coordinate that locate the center of the frame in the full detector frame
    date_collected =  None, #Date data was collected
    date_processed =  None, #Date data was processed
    exposure_time =  None, #Exposure time in seconds
    preparation_basepath =  None, #Base path used for the data preparation
    preparation_other =  None, #Other information about preparation (may be beamline-specific)
    shape =  (10,96,96), #The shape of data array (3D!!)
    positions = None, #Positions (if possible measured) of the sample for each frame in meters
    positions_theory = None, #Expected positions of the sample in meters
    scan_command =  None #The command entered to run the scan
)

DEFAULT_DATA = u.Param(
    sourcetype = 'static',   # Source type: 'static' or 'dynamic' (only 'static' is supported for the moment)
    filelist = [],           # File list to load data from
    unique_scan_labels = True,    # If True, raise an error if multiple scans have the same name.
)

class DataScan(object):
    """\
    DataScan: A single ptychography scan, created on the fly or read from file.
    
    Main methods:
     - set_source: set a source file and load meta information (scan_info)
     - load: Loads data in memory
     - save: Store data and meta data on file
     
    Main attibutes:
     - scan_info: meta data on this scan - all physical distances in meters
     - data, mask, dark, flat: 3D or 2D arrays containing the data (mask, etc) frames.
     - datalist, masklist, darklist, flatlist: lists of frames giving access to the data 
       in a uniform way, and compatible with MPI sharing (datalist[i] is None if the ith
       frame in the scan is not managed by the current node).   
    """
    
    DEFAULT_scan_info=DEFAULT_scan_info

    def __init__(self, pars=None, source=None, sink=None):
        """\
        DataScan: A single ptychography scan.
        
        Parameters
        ----------
        pars   : Param or dict
                 scan_info (meta data) will be updated from this input
        source : str or None or dict
                   The filename to read from. If None, create an empty
                   structure. This empty structue will still have a functional load function
                   but just deliver zeros for data and dark, ones for mask and flat.
        sink : str or None
                File destination to write to. (This is not yet really functional)
                If None, defaults to same value as source.
        """

        # Initialize main class attributes
        self.data = None
        self.mask = None
        self.flat = None
        self.dark = None
        self.datalist = []
        self.masklist = []
        self.flatlist = []
        self.darklist = []
        self.indices = None
        self.scan_info = u.Param(self.DEFAULT_scan_info)
        if pars is not None:
            self.scan_info.update(pars)
            
        self.label =  self.scan_info.get('scan_label')
        
        self.sink = sink if sink is not None else source
        

        self.set_source(source)
        #self.set_sink(sink)

    def set_source(self, other_source):
        """
        Set source and load only information (scan_info) from prepared file.
        Onbe could think to later implement only the loadaer in Childs of a general 
        DataScan object
        
        Parameters
        ----------
        other_source : str or None or dict
                   The file name from which to obtain all parameters.
        """
        
        self.source = other_source
        # local reference
        source = self.source
        
        if self.source is None:
            # set a label
            self.label = self.scan_info.get('scan_label')

            # define how data is accessed
            def loader(key,slc=None):
  
                if key in ['flat','mask']:
                    buf = np.ones(self.scan_info.shape)
                else:
                    buf = np.zeros(self.scan_info.shape)
                    
                return buf if slc is None else buf[slc]
                            
        elif str(source)==source:
            # ok that is probably a file. Try
            if source.endswith('.h5') or source.endswith('.ptyd'):
                
                # scan_info is a field in the file - one just needs to load it.
                self.scan_info.update(u.Param(io.h5read(source,'scan_info')['scan_info']))
                
                # Update the filename, just in case the internal one is incompatible
                self.scan_info.data_filename = source
                
                # set a label if there is one
                self.label = self.scan_info.get('scan_label')
                
                # define how data is accessed
                def loader(key,slc=None):

                    return io.h5read(self.source,key,slice=slc)[key]
        
            else:
                # No other file sources yet implemented
                pass
        
        elif hasattr(source,'items'):
            # a dictionary!
            self.scan_info.update(u.asParam(source.get('scan_info',{})))
            
            # set a label if there is one
            self.label = self.scan_info.get('scan_label')
            
            # define how data is accessed
            def loader(key,slc=None):

                return self.source[key] if slc is None else self.source[key][slc]
                
        # attach loader
        self.loader = loader
        
    def load(self, first=0, last=None, roidim=None, roictr=None, MPIsplit=True):
        """\
        Load data (or portion of it). If MPIsplit is True, the data will be divided in contiguous
        blocks among processes using parallel.loadmanager. While self.data
        is a numpy array, seld.datalist is a list as long as the total number
        of frames containing either None if the data is owned by another process
        or view on the corresponding frame in self.data.
        
        Parameters
        ----------
        roidim : None or 2-tuple
                 If not None, the size of a region of interest.
        roictr : None or 2-tuple
                 If not None, the center of the region of interest.
        first :  0, int
                 First data frame to read.
                 If list is provided in MPIsplit, this parameter will not be used
        last :  None or int > 0
                Last data frame to read. If None it will use scan_info.shape[0]
                If list is provided in MPIsplit, this parameter will not be used
        MPIsplit : list, True or False (default: True)
                   If a list in integers, read only the given frames, all others 
                   being set to None. If True, automatically determine which
                   frame to read (using parallel.loadmanager). If False,
                   load all data regardless of MPI state. 
        """
        """
        # I/O
        if filename is None:
            # Filename to read from
            filename = self.scan_info.data_filename
        else:
            # Get or update file metadata if filename is provided
            self.load_info(filename)
        logger.info('Loading data file %s' % filename)
        """
        # only required info here is scan_info.shape
        last = self.scan_info.shape[0] if last is None else last
        Nframes = last - first
        assert Nframes > 0 
        # Diffraction pattern size
        dpsize = expect2(self.scan_info.shape[1:])
        logger.info('Frame size is %dx%d' % tuple(dpsize))
        
        # Indentify which indices to read
        if MPIsplit is False:
            # Read all frames
            indices = range(first,last)
            frame_slice = slice(None)
        elif MPIsplit is True:
            # Split things automatically
            idlist = [(self.scan_info.scan_label, i) for i in range(first,last)]
            indices = parallel.loadmanager.assign(idlist)[parallel.rank]

            # We are supporting only sequential indice assignments. This will fail
            # if the rules change in parallel.loadmanager.
            assert indices==range(indices[0],indices[-1]+1)
            
            frame_slice = slice(indices[0],indices[-1]+1)
            logger.info('Process %d takes data slice %s' % (parallel.rank, str(frame_slice)), extra={'allprocesses':True})
        else:
            # Load the provided indices
            indices = MPIsplit
            frame_slice = None
        
        self.indices = indices
        self.Nframes = Nframes
        self.frame_slice = frame_slice


        # ROI need to be loaded differently
        if roidim is not None:
            
            roidim = expect2(roidim)
            logger.info('Loading a region of interest %dx%d' % tuple(roidim)) 
 
            # Default ctr for the ROI is the center of the frame.
            if roictr is None:
                roictr = np.asarray(dpsize) // 2
            else:
                roictr = expect2(roictr)
            logger.info('Region of interest centered at (%d,%d)' % tuple(roictr))

            # Prepare for slicing
            self.asize = roidim
            roislice = (slice(int(np.ceil(roictr[0] - roidim[0]/2.)), int(np.ceil(roictr[0] + roidim[0]/2.))),
                         slice(int(np.ceil(roictr[1] - roidim[1]/2.)), int(np.ceil(roictr[1] + roidim[1]/2.))) )
        else:
            self.asize = self.scan_info.shape[1:]
            logger.info('Loading full frames (%dx%d)' % tuple(self.asize))
            roislice = (slice(None), slice(None))
            
        # Read data: h5read allows for a slice argument to get only the portion that we need. 
        if frame_slice is not None:
            # Concatenate the frame and roi slices
            sl = (frame_slice,) + roislice
        else:
            # Pass the list of indices
            sl = (tuple(indices),) + roislice

        #data = self.loader('data', slice=sl)
        #logger.debug('Process %d - loaded data with argument "slice=%s"' % (parallel.rank, str(sl)), extra={'allprocesses':True})
        
        def probe_n_load(key,slc, altkey=None):
            if key is None: return None
            try:
                a = self.loader(key,slc=slc)
                logger.debug('Process %d - Loaded %s with argument "slice=%s"' % (parallel.rank,key,str(slc)), extra={'allprocesses':True})
                return a
            except (IndexError,TypeError): # catch all esceptions associated with slice access here.
                logger.debug('Process %d - Index error for "%s", Reducing slice by 1 dimension', (parallel.rank,str(slc)), extra={'allprocesses':True})
                a = probe_n_load(key,slc[1:])
                return a
            except KeyError:
                if altkey is None:
                    logger.debug('Process %d - No %s frame(s) were found' % (parallel.rank,key), extra={'allprocesses':True}) 
                    return None
                else:
                    logger.debug('Process %d - Trying alternate key %s' % (parallel.rank,altkey), extra={'allprocesses':True})
                    a = probe_n_load(altkey,slc)
                return a
                
        """
        # Load the mask. For backward compatibility, we need to try "fmask" and "mask"
        # We also need to check if the mask is 2D or 3D. 
        try:
            m0 = io.h5read(filename, 'mask[0]')['mask']
            maskname = 'mask'
        except KeyError:
            m0 = io.h5read(filename, 'fmask[0]')['fmask']
            maskname = 'fmask'

        # Stored mask is a 2D frame if the slice just loaded is 1D.
        mask_is_2D = (m0.ndim==1)
        mask_sl = roislice if mask_is_2D else sl
        mask = io.h5read(filename, maskname, slice=mask_sl)[maskname]
        logger.debug('Loaded mask ("%s") with argument "slice=%s"' % (maskname, str(mask_sl)))
    
        # Load a flat frame, if available. 
        try:
            flat = io.h5read(filename, 'flat', slice=roislice)['flat']
            logger.debug('Loaded flat with argument "slice=%s"' % str(roislice))
        except KeyError:
            flat = None
            logger.info('No flat frame was found.')

        # Load a dark frame, if available. 
        try:
            dark = io.h5read(filename, 'dark', slice=roislice)['dark']
            logger.debug('Loaded dark frame with argument "slice=%s"' % str(roislice))
        except KeyError:
            dark = None
            logger.info('No dark frame was found.')
        """
        
        self.data = probe_n_load('data',sl)
        self.mask = probe_n_load('mask',sl,'fmask')
        self.dark = probe_n_load('dark',sl)
        self.flat = probe_n_load('flat',sl)

        # Populate the flat lists
        self.datalist = [None] * Nframes 
        self.masklist = [None] * Nframes  
        self.flatlist = [None] * Nframes  
        self.darklist = [None] * Nframes  
        for k,i in enumerate(indices):
            self.datalist[i] = self.data[k]
            self.masklist[i] = self.mask if self.mask.ndim==2 else self.mask[k]
            self.flatlist[i] = self.flat #if self.flat.ndim==2 else self.flat[k]
            self.darklist[i] = self.dark #if self.dark.ndim==2 else self.dark[k]

        #print [n is not None for n in self.datalist]
        
    def unload_data(self):
        """\
        Deletes the numpy arrays. This might not be very efficient
        if references to these lists or their content exist somewhere else.        
        """
        del self.datalist, self.masklist, self.darklist, self.flatlist
        # Could check with sys.getrefcount(self.data) if it is at all useful to delete it.
        del self.data, self.mask, self.dark, self.flat

    def save(self, filename=None, force_overwrite=True):
        """\
        Store the dataset in a standard format.
        
        Parameters
        ----------
        filename : str or None (default)
                   File to write to. If None, use default (scan_info.data_filename)
        force_overwrite : True (default) or False or None
                          If True the file will be saved even if it already exists.
                          If None the user is asked to confirm.
                          
        NOTE: this function is MPI compatible: it will join all pieces together
        in the master node before writing. BUT: it is assumed that all data is
        spread 
        """
        # Check if data is available
        if not hasattr(self, 'data'):
            raise RuntimeError("Attempting to save DataScan instance that does not contain data.")

        # There is some work to do here if the data is distributed among many processes
        # In the long run consider using hdf5 support to do this more cleanly.
        
        # If data was attached dynamically it is possible that datalist is not consistent)
        if len(self.data) != len(self.datalist):
            if parallel.MPIenabled:
                raise RuntimeError('Inconsistent datalist while running MPI.')
            Nframes = len(self.data)
            
        if parallel.MPIenabled:
            for i in range(Nframes):
                if parallel.master:

                    # Root receives the data if it doesn't have it yet
                    if self.datalist[i] is None:
                        self.datalist[i] = parallel.receive()
                        
                    # The barrier is needed to make sure that we receive data in the right order
                    parallel.barrier()

                else:
                    if self.datalist[i] is not None:
                        # Send data to root.
                        parallel.send(self.datalist[i])

                    parallel.barrier()
                    
            parallel.barrier()
            for i in range(Nframes):
                if parallel.master:

                    # Root receives the data if it doesn't have it yet
                    if self.masklist[i] is None:
                        self.masklist[i] = parallel.receive()
                        
                    # The barrier is needed to make sure that we receive data in the right order
                    parallel.barrier()

                else:
                    if self.masklist[i] is not None:
                        # Send data to root.
                        parallel.send(self.masklist[i])

                    parallel.barrier()

        # All the rest is done by the master node
        if parallel.rank > 0: 
            parallel.barrier()
            return
        
        if parallel.MPIenabled:
            # Transform into a numpy array for saving.
            data = np.asarray(self.datalist)
            mask = np.asarray(self.masklist)
        else:
            data = self.data
            mask = self.mask

        # Sanity check
        if data.shape != self.scan_info.shape:
            error_string = "Attempting to save DataScan instance with non-native data dimension "
            error_string += "[data.shape = %s, while scan_info.shape = %s]" % (str(data.shape), str(self.scan_info.shape))
            raise RuntimeError(error_string)
            
        # Sanity check for mask too?
        # Filename to save to
        if filename is None:
            filename = self.scan_info.data_filename           

        filename = clean_path(filename)
        if os.path.exists(filename):
            if force_overwrite:
                logger.warn('Save file exists but will be overwritten (force_overwrite is True)')
            elif not force_overwrite:
                raise RuntimeError('File %s exists! Operation cancelled.' % filename)
            elif force_overwrite is None:
                ans = raw_input('File %s exists! Overwrite? [Y]/N' % filename)
                if ans and ans.upper() != 'Y':
                    raise RuntimeError('Operation cancelled by user.') 

        # Store using h5write - will be changed at some point
        h5opt = io.h5options['UNSUPPORTED']
        io.h5options['UNSUPPORTED'] = 'ignore'
        io.h5write(filename, data=data, mask=mask, flat=self.flat, dark=self.dark, scan_info=self.scan_info._to_dict())
        io.h5options['UNSUPPORTED'] = h5opt
        logger.info('Scan %s data saved to %s.' % (self.scan_info.scan_label, filename))
        
        parallel.barrier()
        return

    def as_data_package(self,start=0,stop=None):
        """
        returns a part of a DataScan in the format expect by model.new_data()
        """
        if stop is None or stop >self.Nframes:
            stop = self.Nframes
            
        outdict = u.Param()
        outdict.common = u.Param(MT.as_meta(self.scan_info))
        outdict.common.label = self.label
        outdict.iterable=[]
        for i in range(start,stop):
            dct={}
            dct['data']=self.datalist[i]
            dct['mask']=self.masklist[i]
            dct['index']=i
            dct['position']=self.scan_info.positions[i]
            outdict.iterable.append(dct)
            
        return outdict
        
class StaticDataSource(object):
    """
    Static Data Source: for completed scans entirely available on disk.
    """
    
    def __init__(self, sources, pars_list, recon_labels):
        """
        Static Data Source: for completed scans entirely available on disk.
        
        Parameters:
        -----------
        filelist : list of str
                   The filenames to load data from.
        recon_labels : list of str
                    Label that the reconstruction algorithm assigns to the scans
        """
        # Store file list
        self.sources = sources

        self.recon_labels = recon_labels

        # Prepare to load all datasets
        self.scans = {}
        self.labels = []
        Nframes = 0
        for i, source in enumerate(sources):
            # Create en empty structure and get only the meta-information
            DS = DataScan(pars=pars_list[i])
            DS.set_source(source)
            
            # Append the name of this scan to the list - useful to treat them
            # In the order they came.
            label = self.recon_labels[i] if i < len(self.recon_labels) else None
            if label is None or label in self.labels:
                label = DS.label % {'idx':i}
                
            if label in self.labels:
                raise RuntimeError('Scan label "%s" is not unique! Are you loading the same data twice?\n Please assign a different internal label' % scan_label)
            
            DS.label = label
            #self.scan_labels.append(scan_label)
            
            # Store the DataScan object
            self.scans[label] = DS 
            
            # Save information that could be useful
            Nframes += DS.scan_info.shape[0]
            
        # Total number of frames
        self.Nframes = Nframes

        # Set a flag to inform that data is available.
        self.data_available = (self.Nframes > 0)
        
        self.count = 0
        
    def feed_data(self):
        """
        Generator aggregating data of multiple scans.
        
        For now, simply pass the complete DataScan object.
        """
        # Loop through scans
        for label,DS in self.scans.iteritems():
            
            # Load the data from disk only at this point
            DS.load()

            outdict = DS.as_data_package()
            parallel.barrier()
            yield outdict

            # Try to free memory
            DS.unload_data()
            
        # Just in case, a flag that says that we are done.
        self.data_available = False
        #self.data_available = (self.count < self.Nframes)

class PseudoDynamicDataSource(StaticDataSource):
    """
    Pseudo dynamic Data Source that feeds as much diffraction patterns
    as specified with `patterns_per_call`
    
    Prepares and creates Data Scan objects just like StaticDataSource 
    from which it inherits.
    """
    def __init__(self, filelist, unique_scan_labels,patterns_per_call=5):
        super(DynamicDataSource,self).__init__(filelist, unique_scan_labels)
        self.ppc = patterns_per_call
        self.calls = 0
        self.frame_count = 0
        self.scan_items = self.scans.items()
        self.scan_item = self.scan_items.pop(0)   
             
    def feed_data(self):
        
        for label,DS in self.scans.iteritems():
            
            # Load the data from disk only at this point
            DS.load_data()

            # overwrite the label in scan_info
            DS.scan_info.scan_label = label
            #for calls in range(0,min(DS.frames // self.ppc,1)):
            outdict = DS.as_data_package(self.calls*self.ppc,(self.calls+1)*self.ppc)
            self.calls +=1
            self.frame_count += len(outdict.iterable)
            yield outdict

            # Try to free memory
            DS.unload_data()
            
        # Just in case, a flag that says that we are done.
        
        #self.data_available = False
        self.data_available = (self.frame_count < self.Nframes)

        
            
DEFAULT = DEFAULT_DATA
def make_datasource(ptycho, pars=None):
    """
    Produce a DataSource object according to given parameters. For now this
    function merely creates an instance of StaticDataSource, but this could
    change when streaming data in real time.
    """
    # Prepare input parameters
    p = u.Param(make_datasource.DEFAULT)
    if pars is not None: p.update(pars)
    
    if p.sourcetype != 'static':
        return PseudoDynamicDataSource(p.filelist, p.unique_scan_labels)
        #raise ValueError("Only data sources of type 'static' are supported")
    
    # Create and return the data source
    return StaticDataSource(p.filelist, p.unique_scan_labels)
    
# Attach default parameters as function attribute
make_datasource.DEFAULT = DEFAULT_DATA

class MetaTranslator(object):
    """
    Le Traducteur
    """
    PAIRS = dict(
    scan_label = 'label_original', 
    wavelength = 'lam' ,
    detector_distance = 'z',    
    detector_pixel_size = 'psize_det',   
    )
    
    def __init__(self):
        self.to_meta_dct = dict(DEFAULT_scan_info.copy())
        # reaplace all values bye keys:
        for k in self.to_meta_dct.keys():
            self.to_meta_dct[k]=k
        # update with translations
        self.to_meta_dct.update(self.PAIRS)
        
        # invert it
        self.to_scan_info_dct = {v:k for k,v in self.to_meta_dct.iteritems()}
    
    def as_meta(self,key):
        if hasattr(key,'items'):
            new ={}
            for k,v in key.items():
                newk = self.as_meta(k)
                if newk is None:
                    # uh an unknown key. we skip
                    continue
                else:
                    new[newk]=v
            return new
        else:
            return self.to_meta_dct.get(key)
    
    def as_scan_info(self,key):
        if hasattr(key,'items'):
            new ={}
            for k,v in key.items():
                newk = self.as_scan_info(k)
                if newk is None:
                    # uh an unknown key. we skip
                    continue
                else:
                    new[newk]=v
            return new
        else:
            return self.to_scan_info_dct.get(key)
            
MT = MetaTranslator()
"""
class DynamicDataSource(StaticDataSource):
    
    #Pseudo Data Source for real-time data feeds.

    #Note : This is not even beta. Very unstable. 
      
    def __init__(self, filelist, unique_scan_labels,delay=0.5):
        super(DynamicDataSource,self).__init__(filelist, unique_scan_labels)
        self._thread = None
        self._buff = [u.Param()] * len(self.scan_labels)
        self._active = 0
        self.delay = delay
        self.last_index = 0

        self.activate()
        # little head start for the thread:
        time.sleep(12)
        
    def activate(self):

        self._stopping = False
        self._thread = Thread(target=self._ct)
        self._thread.daemon = True
        self._thread.start()
        
    def _ct(self):
        
        for num,label in enumerate(self.scan_labels):
            outdict = self._buff[num]
            self._active = num
            DS = self.scans[label]
            # Load the data from disk only at this point
            DS.load_data()

            # overwrite the label in scan_info
            DS.scan_info.scan_label = label
            try:
                del DS.scan_info['simulation']
            except:
                pass
            # maybe filter better here in future. scan_info can consist of quite a bit of other data
            outdict.common = DS.scan_info.copy()
            outdict.common.lam = DS.scan_info.wavelength
            outdict.common.z = DS.scan_info.detector_distance
            outdict.common.psize_det = DS.scan_info.detector_pixel_size
            outdict.iterable=[]
            for i in range(len(DS.datalist)):
                dct={}
                dct['data']=DS.datalist[i]
                dct['mask']=DS.masklist[i]
                dct['index']=i
                dct['position']=DS.scan_info.positions[i]
                outdict.iterable.append(dct)
                self.data_available = True
                time.sleep(self.delay)
            
            # Try to free memory
            DS.unload_data()
            
        # Just in case, a flag that says that we are done.
        self.data_available = False
            
    def feed_data(self):
        new_last_index = len(self._buff[self._active]['iterable'])
        iterable = self._buff[self._active]['iterable'][self.last_index:new_last_index]
        self.last_index = new_last_index
        common = self._buff[self._active]['common']
        out = u.Param()
        out.iterable=iterable
        out.common = common
        self.data_available = False
        yield out.copy()

"""
"""
# Loop through frames
for i in range(DS.Nframes):
    # Empty structure
    outdct = u.Param()
    
    # Data frame - could be None in MPI
    outdct.data = DS.datalist[i]
    outdct.mask = DS.masklist[i]
    outdct.flat = DS.flatlist[i]
    outdct.dark = DS.darklist[i]
     
    # The metadata is identical to scan_info with a few exceptions
    si = DS.scan_info.copy()
    
    # The frame is related to a single position, so we remove the list
    # and replace it with a single coordinate
    del si.positions
    del si.positions_theory
    # for now we assume only the object to be moving
    pos = DS.scan_info.positions[i]
    post = DS.scan_info.positions_theory[i]
    si.position =np.array([(0.0,0.0), pos,(0.0,0.0)]) if pos is not None else None
    si.position_theory = np.array([(0.0,0.0), post ,(0.0,0.0)]) if post is not None else None
    
    # The frame is 2D, so the shape is adapted accordingly
    si.shape = si.shape[-2:]

    # Keep the scanpoint index for later 
    si.index = i
    
    outdct.update({'meta':si})
    
    # Generator output
    yield outdct
"""
            
