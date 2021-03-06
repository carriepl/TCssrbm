"""

This file extends the mu-ssRBM for tiled-convolutional training

"""
import cPickle, pickle
import numpy
numpy.seterr('warn') #SHOULD NOT BE IN LIBIMPORT
from PIL import Image
import theano
from theano import tensor
from theano.tensor import nnet,grad
from pylearn.io import image_tiling
from pylearn.algorithms.mcRBM import (
        contrastive_cost, contrastive_grad)
import pylearn.gd.sgd

import sys
from unshared_conv_diagonally import FilterActs
from unshared_conv_diagonally import WeightActs
from unshared_conv_diagonally import ImgActs
from Brodatz import Brodatz_op

#import scipy.io
import os
_temp_data_path_ = '.'#'/Tmp/luoheng'

if 1:
    print 'WARNING: using SLOW rng'
    RandomStreams = tensor.shared_randomstreams.RandomStreams
else:
    import theano.sandbox.rng_mrg
    RandomStreams = theano.sandbox.rng_mrg.MRG_RandomStreams


floatX=theano.config.floatX
sharedX = lambda X, name : theano.shared(numpy.asarray(X, dtype=floatX),
        name=name)

def Toncv(image,filters,module_stride=1):
    op = FilterActs(module_stride)
    return op(image,filters)
    
def Tdeconv(filters, hidacts, irows, icols, module_stride=1):
    op = ImgActs(module_stride)
    return op(filters, hidacts, irows, icols)


def unnatural_sgd_updates(params, grads, stepsizes, tracking_coef=0.1, epsilon=1):
    grad_means = [theano.shared(numpy.zeros_like(p.get_value(borrow=True)))
            for p in params]
    grad_means_sqr = [theano.shared(numpy.ones_like(p.get_value(borrow=True)))
            for p in params]
    updates = dict()
    for g, gm, gms, p, s in zip(
            grads, grad_means, grad_means_sqr, params, stepsizes):
        updates[gm] = tracking_coef * g + (1-tracking_coef) * gm
        updates[gms] = tracking_coef * g*g + (1-tracking_coef) * gms

        var_g = gms - gm**2
        # natural grad doesn't want sqrt, but i found it worked worse
        updates[p] = p - s * gm / tensor.sqrt(var_g+epsilon)
    return updates
"""
def grad_updates(params, grads, stepsizes):
    grad_means = [theano.shared(numpy.zeros_like(p.get_value(borrow=True)))
            for p in params]
    grad_means_sqr = [theano.shared(numpy.ones_like(p.get_value(borrow=True)))
            for p in params]
    updates = dict()
    for g, p, s in zip(
            grads, params, stepsizes):
        updates[p] = p - s*g
    return updates
"""
def safe_update(a, b):
    for k,v in dict(b).iteritems():
        if k in a:
            raise KeyError(k)
        a[k] = v
    return a
    
def most_square_shape(N):
    """rectangle (height, width) with area N that is closest to sqaure
    """
    for i in xrange(int(numpy.sqrt(N)),0, -1):
        if 0 == N % i:
            return (i, N/i)


def tile_conv_weights(w,flip=False, scale_each=False):
    """
    Return something that can be rendered as an image to visualize the filters.
    """
    #if w.shape[1] != 3:
    #    raise NotImplementedError('not rgb', w.shape)
    if w.shape[2] != w.shape[3]:
        raise NotImplementedError('not square', w.shape)

    if w.shape[1] == 1:
	wmin, wmax = w.min(), w.max()
    	if not scale_each:
            w = numpy.asarray(255 * (w - wmin) / (wmax - wmin + 1e-6), dtype='uint8')
    	trows, tcols= most_square_shape(w.shape[0])
    	outrows = trows * w.shape[2] + trows-1
    	outcols = tcols * w.shape[3] + tcols-1
    	out = numpy.zeros((outrows, outcols), dtype='uint8')
    	#tr_stride= 1+w.shape[1]
    	for tr in range(trows):
            for tc in range(tcols):
            	# this is supposed to flip the filters back into the image
            	# coordinates as well as put the channels in the right place, but I
            	# don't know if it really does that
            	tmp = w[tr*tcols+tc,
			     0,
                             ::-1 if flip else 1,
                             ::-1 if flip else 1]
            	if scale_each:
                    tmp = numpy.asarray(255*(tmp - tmp.min()) / (tmp.max() - tmp.min() + 1e-6),
                        dtype='uint8')
            	out[tr*(1+w.shape[2]):tr*(1+w.shape[2])+w.shape[2],
                    tc*(1+w.shape[3]):tc*(1+w.shape[3])+w.shape[3]] = tmp
    	return out

    wmin, wmax = w.min(), w.max()
    if not scale_each:
        w = numpy.asarray(255 * (w - wmin) / (wmax - wmin + 1e-6), dtype='uint8')
    trows, tcols= most_square_shape(w.shape[0])
    outrows = trows * w.shape[2] + trows-1
    outcols = tcols * w.shape[3] + tcols-1
    out = numpy.zeros((outrows, outcols,3), dtype='uint8')

    tr_stride= 1+w.shape[1]
    for tr in range(trows):
        for tc in range(tcols):
            # this is supposed to flip the filters back into the image
            # coordinates as well as put the channels in the right place, but I
            # don't know if it really does that
            tmp = w[tr*tcols+tc].transpose(1,2,0)[
                             ::-1 if flip else 1,
                             ::-1 if flip else 1]
            if scale_each:
                tmp = numpy.asarray(255*(tmp - tmp.min()) / (tmp.max() - tmp.min() + 1e-6),
                        dtype='uint8')
            out[tr*(1+w.shape[2]):tr*(1+w.shape[2])+w.shape[2],
                    tc*(1+w.shape[3]):tc*(1+w.shape[3])+w.shape[3]] = tmp
    return out

class RBM(object):
    """
    Light-weight class that provides math related to inference in Gaussian RBM

    Attributes:
    - v_shape - the input image shape  (ie. n_imgs, n_chnls, n_img_rows, n_img_cols)

     - n_conv_hs - the number of spike and slab hidden units
     - filters_hs_shape - the kernel filterbank shape for hs units
     - filters_h_shape -  the kernel filterbank shape for h units
     - filters_hs - a tensor with shape (n_conv_hs,n_chnls,n_ker_rows, n_ker_cols)
     - conv_bias_hs - a vector with shape (n_conv_hs, n_out_rows, n_out_cols)
     - subsample_hs - how to space the receptive fields (dx,dy)

     - n_global_hs - how many globally-connected spike and slab units
     - weights_hs - global weights
     - global_bias_hs -

     - _params a list of the attributes that are shared vars


    The technique of combining convolutional and global filters to account for border effects is
    borrowed from  (Alex Krizhevsky, TR?, October 2010).
    """
    def __init__(self, **kwargs):
        print 'init rbm'
	self.__dict__.update(kwargs)

    @classmethod
    def alloc(cls,
            conf,
            image_shape,  # input dimensionality
            filters_hs_shape,       
            filters_irange,
            sigma,            
            seed = 8923402,            
            ):
 	rng = numpy.random.RandomState(seed)

        self = cls()
       
	n_images, n_channels, n_img_rows, n_img_cols = image_shape
        n_filters_hs_modules, n_filters_hs_per_modules, fcolors, n_filters_hs_rows, n_filters_hs_cols = filters_hs_shape        
        assert fcolors == n_channels        
        self.sigma = sigma
        self.v_shape = image_shape
        print 'v_shape'
	print self.v_shape
	self.filters_hs_shape = filters_hs_shape
        print 'self.filters_hs_shape'
        print self.filters_hs_shape
        self.out_conv_hs_shape = FilterActs.infer_shape_without_instance(self.v_shape,self.filters_hs_shape)        
        print 'self.out_conv_hs_shape'
        print self.out_conv_hs_shape
        conv_bias_hs_shape = (n_filters_hs_modules,n_filters_hs_per_modules)
        self.conv_bias_hs_shape = conv_bias_hs_shape
        print 'self.conv_bias_hs_shape'
        print self.conv_bias_hs_shape
        bias_v_shape = self.v_shape[1:]
        self.bias_v_shape = bias_v_shape
        print 'self.bias_v_shape'
        print self.bias_v_shape
        
                
        self.filters_hs = sharedX(rng.randn(*filters_hs_shape) * filters_irange , 'filters_hs')  
        
        #conv_bias_ival = rng.rand(*conv_bias_hs_shape)*2-1
        #conv_bias_ival *= conf['conv_bias_irange']
        #conv_bias_ival += conf['conv_bias0']
        conv_bias_ival = numpy.zeros(conv_bias_hs_shape)
	self.conv_bias_hs = sharedX(conv_bias_ival, name='conv_bias_hs')
	self.bias_v = sharedX(numpy.zeros(self.bias_v_shape), name='bias_v')       
        
        negsample_mask = numpy.zeros((n_channels,n_img_rows,n_img_cols),dtype=floatX)
 	negsample_mask[:,n_filters_hs_rows:n_img_rows-n_filters_hs_rows+1,n_filters_hs_cols:n_img_cols-n_filters_hs_cols+1] = 1
	self.negsample_mask = sharedX(negsample_mask,'negsample_mask')                
        
        self.conf = conf
        self._params = [self.filters_hs,
                self.conv_bias_hs,
                self.bias_v
                ]
        return self
   
    def convdot(self, image, filters):
        return Toncv(image,filters)
        
    def convdot_T(self, filters, hidacts):
        n_images, n_channels, n_img_rows, n_img_cols = self.v_shape
        return Tdeconv(filters, hidacts, n_img_rows, n_img_cols)         

    #####################
    # spike-and-slab convolutional hidden units
    def mean_convhs_h_given_v(self, v):
        """Return the mean of binary-valued hidden units h, given v
        """
        W = self.filters_hs
        vW = self.convdot(v, W)
        vW = vW/self.sigma
        vWb = vW.dimshuffle(0,3,4,1,2) + self.conv_bias_hs
	rval = nnet.sigmoid(vWb.dimshuffle(0,3,4,1,2))
        return rval

   
    #####################
    # visible units
    def mean_v_given_h(self, convhs_h):
        Wh = self.convdot_T(self.filters_hs, convhs_h)
        rval = Wh + self.bias_v
        return rval*self.sigma 
 
    #####################

    def gibbs_step_for_v(self, v, s_rng, return_locals=False):
        #positive phase

        mean_convhs_h = self.mean_convhs_h_given_v(v)
        
        def sample_h(hmean,shp):
            return tensor.cast(s_rng.uniform(size=shp) < hmean, floatX)
        
        sample_convhs_h = sample_h(mean_convhs_h, self.out_conv_hs_shape)
        
        vv_mean = self.mean_v_given_h(sample_convhs_h)
        
        vv_sample = s_rng.normal(size=self.v_shape)*self.sigma + vv_mean
        vv_sample = theano.tensor.mul(vv_sample,self.negsample_mask)
       
	if return_locals:
            return vv_sample, locals()
        else:
            return vv_sample

    def free_energy_given_v(self, v):
        # This is accurate up to a multiplicative constant
        # because I dropped some terms involving 2pi
        def pre_sigmoid(x):
            assert x.owner and x.owner.op == nnet.sigmoid
            return x.owner.inputs[0]

        pre_convhs_h = pre_sigmoid(self.mean_convhs_h_given_v(v))
        rval = tensor.add(
                -tensor.sum(nnet.softplus(pre_convhs_h),axis=[1,2,3,4]), #the shape of pre_convhs_h: 64 x 11 x 32 x 8 x 8
                (0.5/self.sigma) * tensor.sum((v-self.bias_v)**2, axis=[1,2,3]), #shape: 64 x 1 x 98 x 98 
                )
        assert rval.ndim==1
        return rval

    def cd_updates(self, pos_v, neg_v, stepsizes, other_cost=None):
        grads = contrastive_grad(self.free_energy_given_v,
                pos_v, neg_v,
                wrt=self.params(),
                other_cost=other_cost
                ) 
        assert len(stepsizes)==len(grads)

        if self.conf['unnatural_grad']:
            sgd_updates = unnatural_sgd_updates
        else:
            sgd_updates = pylearn.gd.sgd.sgd_updates
        rval = dict(
                sgd_updates(
                    self.params(),
                    grads,
                    stepsizes=stepsizes))
        if 0:
            #DEBUG STORE GRADS
            grad_shared_vars = [sharedX(0*p.value.copy(),'') for p in self.params()]
            self.grad_shared_vars = grad_shared_vars
            rval.update(dict(zip(grad_shared_vars, grads)))
       
	return rval

    def params(self):
        # return the list of *shared* learnable parameters
        # that are, in your judgement, typically learned in this model
        return list(self._params)

    def save_weights_to_files(self, identifier):
        # save 4 sets of weights:
        pass
    def save_weights_to_grey_files(self, identifier):
        # save 4 sets of weights:

        #filters_hs
        def arrange_for_show(filters_hs,filters_hs_shape):
	    n_filters_hs_modules, n_filters_hs_per_modules, fcolors, n_filters_hs_rows, n_filters_hs_cols  = filters_hs_shape            
            filters_fs_for_show = filters_hs.reshape(
                       (n_filters_hs_modules*n_filters_hs_per_modules, 
                       fcolors,
                       n_filters_hs_rows,
                       n_filters_hs_cols))
            fn = theano.function([],filters_fs_for_show)
            rval = fn()
            return rval
        filters_fs_for_show = arrange_for_show(self.filters_hs, self.filters_hs_shape)
        Image.fromarray(
                       tile_conv_weights(
                       filters_fs_for_show,flip=False), 'L').save(
                'filters_hs_%s.png'%identifier)
      
    def dump_to_file(self, filename):
        try:
            cPickle.dump(self, open(filename, 'wb'))
        except cPickle.PicklingError:
            pickle.dump(self, open(filename, 'wb'))


class Gibbs(object): # if there's a Sampler interface - this should support it
    @classmethod
    def alloc(cls, rbm, batchsize, rng):
        if not hasattr(rng, 'randn'):
            rng = numpy.random.RandomState(rng)
        self = cls()
        seed=int(rng.randint(2**30))
        self.rbm = rbm
	if batchsize==rbm.v_shape[0]:
	    self.particles = sharedX(
            rng.randn(*rbm.v_shape),
            name='particles')
	else:
	    self.particles = sharedX(
            rng.randn(batchsize,1,98,98),
            name='particles')
        self.s_rng = RandomStreams(seed)
        return self

def HMC(rbm, batchsize, rng): # if there's a Sampler interface - this should support it
    if not hasattr(rng, 'randn'):
        rng = numpy.random.RandomState(rng)
    seed=int(rng.randint(2**30))
    particles = sharedX(
            rng.randn(*rbm.v_shape),
            name='particles')
    return pylearn.sampling.hmc.HMC_sampler(
            particles,
            rbm.free_energy_given_v,
            seed=seed)


class Trainer(object): # updates of this object implement training
    @classmethod
    def alloc(cls, rbm, visible_batch,
            lrdict,
            conf,
            rng=234,
            iteration_value=0,
            ):

        batchsize = rbm.v_shape[0]
        sampler = Gibbs.alloc(rbm, batchsize, rng=rng)
	print 'alloc trainer'
        error = 0.0
        return cls(
                rbm=rbm,
                batchsize=batchsize,
                visible_batch=visible_batch,
                sampler=sampler,
                iteration=sharedX(iteration_value, 'iter'),
                learn_rates = [lrdict[p] for p in rbm.params()],
                conf=conf,
                annealing_coef=sharedX(1.0, 'annealing_coef'),
                conv_h_means = sharedX(numpy.zeros(rbm.out_conv_hs_shape[1:])+0.5,'conv_h_means'),
                cpnv_h       = sharedX(numpy.zeros(rbm.out_conv_hs_shape), 'conv_h'),
                #recons_error = sharedX(error,'reconstruction_error'),                
                )

    def __init__(self, **kwargs):
        print 'init trainer'
	self.__dict__.update(kwargs)

    def updates(self):
        
        print 'start trainer.updates'
	conf = self.conf
        ups = {}
        add_updates = lambda b: safe_update(ups,b)

        annealing_coef = 1.0 - self.iteration / float(conf['train_iters'])
        ups[self.iteration] = self.iteration + 1 #
        ups[self.annealing_coef] = annealing_coef

        conv_h = self.rbm.mean_convhs_h_given_v(
                self.visible_batch)
        
        
        new_conv_h_means = 0.1 * conv_h.mean(axis=0) + .9*self.conv_h_means
        #new_conv_h_means = conv_h.mean(axis=0)
        ups[self.conv_h_means] = new_conv_h_means
        ups[self.cpnv_h] = conv_h
        #ups[self.global_h_means] = new_global_h_means


        #sparsity_cost = 0
        #self.sparsity_cost = sparsity_cost
        # SML updates PCD
        add_updates(
                self.rbm.cd_updates(
                    pos_v=self.visible_batch,
                    neg_v=self.sampler.particles,
                    stepsizes=[annealing_coef*lr for lr in self.learn_rates]))
        
        if conf['chain_reset_prob']:
            # advance the 'negative-phase' chain
            nois_batch = self.sampler.s_rng.normal(size=self.rbm.v_shape)
            resets = self.sampler.s_rng.uniform(size=(conf['batchsize'],))<conf['chain_reset_prob']
            old_particles = tensor.switch(resets.dimshuffle(0,'x','x','x'),
                    nois_batch,   # reset the chain
                    self.sampler.particles,  #continue chain
                    )
            #old_particles = tensor.switch(resets.dimshuffle(0,'x','x','x'),
            #        self.visible_batch,   # reset the chain
            #        self.sampler.particles,  #continue chain
            #        )
        else:
            old_particles = self.sampler.particles
        tmp_particles = old_particles    
        for step in xrange(self.conf['steps_sampling']):
             tmp_particles  = self.rbm.gibbs_step_for_v(tmp_particles, self.sampler.s_rng)
        new_particles = tmp_particles       
        #broadcastable_value = new_particles.broadcastable
        #print broadcastable_value
        #reconstructions= self.rbm.gibbs_step_for_v(self.visible_batch, self.sampler.s_rng)
	#recons_error   = tensor.sum((self.visible_batch-reconstructions)**2)
	#recons_error = 0.0
        #ups[self.recons_error] = recons_error
	#return {self.particles: new_particles}
        ups[self.sampler.particles] = new_particles
             
        return ups

    def save_weights_to_files(self, pattern='iter_%05i'):
        #pattern = pattern%self.iteration.get_value()

        # save particles
        #Image.fromarray(tile_conv_weights(self.sampler.particles.get_value(borrow=True),
        #    flip=False),
        #        'RGB').save('particles_%s.png'%pattern)
        #self.rbm.save_weights_to_files(pattern)
        pass

    def save_weights_to_grey_files(self, pattern='iter_%05i'):
        pattern = pattern%self.iteration.get_value()

        # save particles
        """
        particles_for_show = self.sampler.particles.dimshuffle(3,0,1,2)
        fn = theano.function([],particles_for_show)
        particles_for_show_value = fn()
        Image.fromarray(tile_conv_weights(particles_for_show_value,
            flip=False),'L').save('particles_%s.png'%pattern)
        self.rbm.save_weights_to_grey_files(pattern)
        """
        Image.fromarray(tile_conv_weights(self.sampler.particles.get_value(borrow=True),
            flip=False),'L').save('particles_%s.png'%pattern)
        self.rbm.save_weights_to_grey_files(pattern)
    def print_status(self):
        def print_minmax(msg, x):
            assert numpy.all(numpy.isfinite(x))
            print msg, x.min(), x.max()

        print 'iter:', self.iteration.get_value()
        print_minmax('filters_hs ', self.rbm.filters_hs.get_value(borrow=True))        
        print_minmax('particles', self.sampler.particles.get_value())
        print_minmax('conv_h_means', self.conv_h_means.get_value())
        print_minmax('conv_h', self.cpnv_h.get_value())
        print_minmax('visible_bais', self.rbm.bias_v.get_value())
        print 'lr annealing coef:', self.annealing_coef.get_value()
	#print 'reconstruction error:', self.recons_error.get_value()

def main_inpaint(filename, algo='Gibbs', rng=777888, scale_separately=False):
    rbm = cPickle.load(open(filename))
    sampler = Gibbs.alloc(rbm, rbm.conf['batchsize'], rng)
    
    batch_idx = tensor.iscalar()
    batch_range = batch_idx * rbm.conf['batchsize'] + numpy.arange(rbm.conf['batchsize'])
    
    n_examples = rbm.conf['batchsize']   #64
    n_img_rows = 98
    n_img_cols = 98
    n_img_channels=1
    batch_x = Brodatz_op(batch_range,
  	                     '../../../Brodatz/D6.gif',   # download from http://www.ux.uis.no/~tranden/brodatz.html
  	                     patch_shape=(n_img_channels,
  	                                 n_img_rows,
  	                                 n_img_cols), 
  	                     noise_concelling=0., 
  	                     seed=3322, 
  	                     batchdata_size=n_examples
  	                     )	
    fn_getdata = theano.function([batch_idx],batch_x)
    batchdata = fn_getdata(0)
    scaled_batchdata = (batchdata - batchdata.min())/(batchdata.max() - batchdata.min() + 1e-6)
    scaled_batchdata[:,:,11:88,11:88] = 0
    
    batchdata[:,:,11:88,11:88] = 0
    print 'the min of border: %f, the max of border: %f'%(batchdata.min(),batchdata.max())
    shared_batchdata = sharedX(batchdata,'batchdata')
    border_mask = numpy.zeros((n_examples,n_img_channels,n_img_rows,n_img_cols),dtype=floatX)
    border_mask[:,:,11:88,11:88]=1
        
    sampler.particles = shared_batchdata
    new_particles = rbm.gibbs_step_for_v(sampler.particles, sampler.s_rng)
    new_particles = tensor.mul(new_particles,border_mask)
    new_particles = tensor.add(new_particles,batchdata)
    fn = theano.function([], [],
                updates={sampler.particles: new_particles})
    particles = sampler.particles


    for i in xrange(500):
        print i
        if i % 20 == 0:
            savename = '%s_inpaint_%04i.png'%(filename,i)
            print 'saving'
            temp = particles.get_value(borrow=True)
            print 'the min of center: %f, the max of center: %f' \
                                 %(temp[:,:,11:88,11:88].min(),temp[:,:,11:88,11:88].max())
            if scale_separately:
	        scale_separately_savename = '%s_inpaint_scale_separately_%04i.png'%(filename,i)
	        blank_img = numpy.zeros((n_examples,n_img_channels,n_img_rows,n_img_cols),dtype=floatX)
	        tmp = temp[:,:,11:88,11:88]
	        tmp = (tmp - tmp.min()) / (tmp.max() - tmp.min() + 1e-6)
	        blank_img[:,:,11:88,11:88] = tmp 
	        blank_img = blank_img + scaled_batchdata
	        Image.fromarray(
                tile_conv_weights(
                    blank_img,
                    flip=False),
                'L').save(scale_separately_savename)
            else:
	        Image.fromarray(
                tile_conv_weights(
                    particles.get_value(borrow=True),
                    flip=False),
                'L').save(savename)
        fn()

def main_sample(filename, algo='Gibbs', rng=777888, burn_in=5000, save_interval=5000, n_files=10):
    rbm = cPickle.load(open(filename))
    if algo == 'Gibbs':
        sampler = Gibbs.alloc(rbm, rbm.conf['batchsize'], rng)
        new_particles  = rbm.gibbs_step_for_v(sampler.particles, sampler.s_rng)
        new_particles = tensor.clip(new_particles,
                rbm.conf['particles_min'],
                rbm.conf['particles_max'])
        fn = theano.function([], [],
                updates={sampler.particles: new_particles})
        particles = sampler.particles
    elif algo == 'HMC':
        print "WARNING THIS PROBABLY DOESNT WORK"
        # still need to figure out how to get the clipping into
        # the iterations of mcmc
        sampler = HMC(rbm, rbm.conf['batchsize'], rng)
        ups = sampler.updates()
        ups[sampler.positions] = tensor.clip(ups[sampler.positions],
                rbm.conf['particles_min'],
                rbm.conf['particles_max'])
        fn = theano.function([], [], updates=ups)
        particles = sampler.positions

    for i in xrange(burn_in):
	print i
	if i % 20 == 0:
            savename = '%s_sample_burn_%04i.png'%(filename,i)
	    print 'saving'
	    Image.fromarray(
                tile_conv_weights(
                    particles.get_value(borrow=True),
                    flip=False),
                'L').save(savename)	
        fn()

    for n in xrange(n_files):
        for i in xrange(save_interval):
            fn()
        savename = '%s_sample_%04i.png'%(filename,n)
        print 'saving', savename
        Image.fromarray(
                tile_conv_weights(
                    particles.get_value(borrow=True),
                    flip=False),
                'L').save(savename)
                
def main_print_status(filename, algo='Gibbs', rng=777888, burn_in=500, save_interval=500, n_files=1):
    def print_minmax(msg, x):
        assert numpy.all(numpy.isfinite(x))
        print msg, x.min(), x.max()
    rbm = cPickle.load(open(filename))
    if algo == 'Gibbs':
        sampler = Gibbs.alloc(rbm, rbm.conf['batchsize'], rng)
        new_particles  = rbm.gibbs_step_for_v(sampler.particles, sampler.s_rng)
        #new_particles = tensor.clip(new_particles,
        #        rbm.conf['particles_min'],
        #        rbm.conf['particles_max'])
        fn = theano.function([], [],
                updates={sampler.particles: new_particles})
        particles = sampler.particles
    elif algo == 'HMC':
        print "WARNING THIS PROBABLY DOESNT WORK"
     
    for i in xrange(burn_in):
	fn()
        print_minmax('particles', particles.get_value(borrow=True))              
                
                
def main0(rval_doc):
    if 'conf' not in rval_doc:
        raise NotImplementedError()

    conf = rval_doc['conf']
    batchsize = conf['batchsize']

    batch_idx = tensor.iscalar()
    batch_range = batch_idx * conf['batchsize'] + numpy.arange(conf['batchsize'])   
       
    if conf['dataset']=='Brodatz':
        n_examples = conf['batchsize']   #64
        n_img_rows = 98
        n_img_cols = 98
        n_img_channels=1
  	batch_x = Brodatz_op(batch_range,
  	                     '../../Brodatz/D6.gif',   # download from http://www.ux.uis.no/~tranden/brodatz.html
  	                     patch_shape=(n_img_channels,
  	                                 n_img_rows,
  	                                 n_img_cols), 
  	                     noise_concelling=0., 
  	                     seed=3322, 
  	                     batchdata_size=n_examples
  	                     )	
    else:
        raise ValueError('dataset', conf['dataset'])     
       
    rbm = RBM.alloc(
            conf,
            image_shape=(        
                n_examples,
                n_img_channels,
                n_img_rows,
                n_img_cols
                ),
            filters_hs_shape=(
                conf['filters_hs_size'],  
                conf['n_filters_hs'],
                n_img_channels,
                conf['filters_hs_size'],
                conf['filters_hs_size']
                ),            #fmodules(stride) x filters_per_modules x fcolors(channels) x frows x fcols
            filters_irange=conf['filters_irange'],    
            sigma=conf['sigma'],
            )

    rbm.save_weights_to_grey_files('iter_0000')

    base_lr = conf['base_lr_per_example']/batchsize
    conv_lr_coef = conf['conv_lr_coef']

    trainer = Trainer.alloc(
            rbm,
            visible_batch=batch_x,
            lrdict={
                # higher learning rate ok with CD1
                rbm.filters_hs: sharedX(conv_lr_coef*base_lr, 'filters_hs_lr'),
                rbm.conv_bias_hs: sharedX(base_lr, 'conv_bias_hs_lr'),
                rbm.bias_v: sharedX(base_lr, 'conv_bias_hs_lr')
                },
            conf = conf,
            )

  
    print 'start building function'
    training_updates = trainer.updates() #
    train_fn = theano.function(inputs=[batch_idx],
            outputs=[],
	    #mode='FAST_COMPILE',
            #mode='DEBUG_MODE',
	    updates=training_updates	    
	    )  #

    print 'training...'
    
    iter = 0
    while trainer.annealing_coef.get_value()>=0: #
        dummy = train_fn(iter) #
        #trainer.print_status()
	if iter % 1000 == 0:
            rbm.dump_to_file(os.path.join(_temp_data_path_,'rbm_%06i.pkl'%iter))
        if iter <= 1000 and not (iter % 100): #
            trainer.print_status()
            trainer.save_weights_to_grey_files()
        elif not (iter % 1000):
            trainer.print_status()
            trainer.save_weights_to_grey_files()
        iter += 1


def main_train():
    print 'start main_train'
    main0(dict(
        conf=dict(
            dataset='Brodatz',
            chain_reset_prob=.0,#approx CD-50
            unnatural_grad=False,
            train_iters=100000,
            base_lr_per_example=0.00001,
            conv_lr_coef=1.0,
            batchsize=64,
            n_filters_hs=32,
            filters_hs_size=11,
            filters_irange=.005,    
            sigma = 0.25,
            n_tiled_conv_offset_diagonally = 1,
            steps_sampling = 1,            
            )))
    

if __name__ == '__main__':
    if sys.argv[1] == 'train':
        sys.exit(main_train())
    if sys.argv[1] == 'sampling':
	sys.exit(main_sample(sys.argv[2]))
    if sys.argv[1] == 'inpaint':
        sys.exit(main_inpaint(sys.argv[2]))
    if sys.argv[1] == 'print_status':
        sys.exit(main_print_status(sys.argv[2]))
