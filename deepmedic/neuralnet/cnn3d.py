# Copyright (c) 2016, Konstantinos Kamnitsas
# All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the BSD license. See the accompanying LICENSE file
# or read the terms at https://opensource.org/licenses/BSD-3-Clause.

from __future__ import absolute_import, print_function, division
import numpy as np
import random
from collections import OrderedDict

import tensorflow as tf

from deepmedic.neuralnet.pathwayTypes import PathwayTypes as pt
from deepmedic.neuralnet.pathways import NormalPathway, SubsampledPathway, FcPathway
from deepmedic.neuralnet.blocks import SoftmaxBlock
import deepmedic.neuralnet.ops as ops
from deepmedic.neuralnet.utils import calc_rec_field_of_path_given_kern_dims_w_stride_1, calc_inp_dims_lr_path_to_match_outp_dims


##################################################
##################################################
################ THE CNN CLASS ###################
##################################################
##################################################

class Cnn3d(object):
    def __init__(self):
        
        self.cnnModelName = None
        
        self.pathways = [] # There should be only 1 normal and only one FC pathway. Eg, see self.getFcPathway()
        self.numSubsPaths = 0
        
        self.finalTargetLayer = ""
        
        self.num_classes = None
        
        #=====================================
        self.recFieldCnn = ""
        
        # self.patchesToTrainPerImagePart = ""
        self.nkerns = ""  # number of feature maps.
        self.nkernsSubsampled = ""
        
        # Fully Connected Layers
        self.kernelDimensionsFirstFcLayer = ""
        
        # Residual Learning
        self.indicesOfLayersToConnectResidualsInOutput = ""
        
        # Lower rank convolutional layers
        self.indicesOfLowerRankLayersPerPathway = ""
        self.ranksOfLowerRankLayersForEachPathway = ""
        
        #======= Input tensors X. Placeholders OR given tensors =======
        # Symbolic variables, which stand for the input. Will be loaded by the compiled trainining/val/test function. Can also be pre-set by an existing tensor if required in future extensions.
        self._inp_x = {'train': {},
                       'val': {},
                       'test': {} }
        
        self._inp_shapes_per_path = {'train': None,
                                    'val': None,
                                    'test': None} # TODO: For sampling. In eager, remove updating calc_inp_dims_of_paths_from_hr_inp
        
        #======= Output tensors Y_GT ========
        # For each targetLayer, I should be placing a y_gt placeholder/feed.
        self._output_gt_tensor_feeds = {'train': {},
                                        'val': {} }
        
        ######## These entries are setup in the setup_train/val/test functions here ############
        self._ops_main = { 'train': {} , 'val': {}, 'test': {} }
        self._feeds_main = { 'train': {} , 'val': {}, 'test': {} }

    
    def get_inp_shapes_per_path(self):
        return self._inp_shapes_per_path # TODO: This is for wrapper. Remove.
    
    def get_inp_shape_of_path(self, path_idx, mode): # Called for sampling. TODO: Remove for eager.
        assert mode in ['train', 'val', 'test']
        return self._inp_shapes_per_path[mode][path_idx]
    
    def getNumSubsPathways(self):
        count = 0
        for pathway in self.pathways :
            if pathway.pType() ==  pt.SUBS :
                count += 1
        return count
    
    def getNumPathwaysThatRequireInput(self):
        count = 0
        for pathway in self.pathways :
            if pathway.pType() != pt.FC :
                count += 1
        return count
    
    def getFcPathway(self):
        for pathway in self.pathways :
            if pathway.pType() == pt.FC :
                return pathway
        return None
    

        
    # for inference with batch-normalization.
    # Every training batch, this is called to update an internal matrix of each layer, with the last mus and vars,
    # so that I can compute the rolling average for inference.
    def update_arrays_of_bn_moving_avg(self, sessionTf):
        for pathway in self.pathways :
            for block in pathway.get_blocks() :
                block.update_arrays_of_bn_moving_avg(sessionTf)  # Will do nothing if no BN.
                    
    def _get_update_ops_for_bn_moving_avg(self) :
        # These are not the variables of the normalization of the FMs' distributions that are optimized during training. These are only the Mu and Stds that are used during inference,
        # ... and here we update the sharedVariable which is used "from the outside during do_training()" to update the rolling-average-matrix for inference. Do for all layers.
        updatesForBnRollingAverage = []
        for pathway in self.pathways :
            for block in pathway.get_blocks() :
                updatesForBnRollingAverage.extend(block.get_update_ops_for_bn_moving_avg())
        return updatesForBnRollingAverage
    
    def get_trainable_params(self, log, indicesOfLayersPerPathwayTypeToFreeze):
        # Called from Trainer.
        paramsToOptDuringTraining = []  # Ws and Bs
        for pathway in self.pathways :
            for block_i in range(len(pathway.get_blocks())) :
                if block_i not in indicesOfLayersPerPathwayTypeToFreeze[ pathway.pType() ] :
                    paramsToOptDuringTraining = paramsToOptDuringTraining + pathway.get_block(block_i).trainable_params()
                else : # Layer will be held fixed. Notice that Batch Norm parameters are still learnt.
                    log.print3("WARN: [Pathway_" + str(pathway.getStringType()) + "] The weights of [Layer-"+str(block_i)+"] will NOT be trained as specified (index, first layer is 0).")
        return paramsToOptDuringTraining
    
    def params_for_L1_L2_reg(self):
        total_params = []
        for pathway in self.pathways:
            for block in pathway.get_blocks():
                total_params += block.params_for_L1_L2_reg()
        return total_params
    
    def get_main_ops(self, str_train_val_test):
        # str_train_val_test: "train", "val" or "test"
        return self._ops_main[str_train_val_test]
    
    def get_main_feeds(self, str_train_val_test):
        return self._feeds_main[str_train_val_test]
    
    
    def setup_ops_n_feeds_to_train(self, log, total_cost, updates_of_params_wrt_total_cost) :
        log.print3("...Building the training function...")
        
        y_gt = self._output_gt_tensor_feeds['train']['y_gt']
        
        #================BATCH NORMALIZATION ROLLING AVERAGE UPDATES======================
        updates = updates_of_params_wrt_total_cost + self._get_update_ops_for_bn_moving_avg()
        updates_grouped_op = tf.group( *updates ) # this op returns no output when run.
        
        #======================== Collecting ops and feeds =================
        log.print3("...Collecting ops and feeds for training...")
        
        self._ops_main['train']['cost'] = total_cost
        self._ops_main['train']['list_rp_rn_tp_tn'] = self.finalTargetLayer.get_rp_rn_tp_tn(self.finalTargetLayer.output['train'], y_gt)
        self._ops_main['train']['updates_grouped_op'] = updates_grouped_op
        
        self._feeds_main['train']['x'] = self._inp_x['train']['x']
        for subpath_i in range(self.numSubsPaths) : # if there are subsampled paths...
            self._feeds_main['train']['x_sub_'+str(subpath_i)] = self._inp_x['train']['x_sub_'+str(subpath_i)]
        self._feeds_main['train']['y_gt'] = y_gt
        
        log.print3("Done.")
        
    def setup_ops_n_feeds_to_val(self, log):
        log.print3("...Building the validation function...")
        
        y_gt = self._output_gt_tensor_feeds['val']['y_gt']
        
        log.print3("...Collecting ops and feeds for validation...")
        
        self._ops_main['val'] = {}
        self._ops_main['val']['list_rp_rn_tp_tn'] = self.finalTargetLayer.get_rp_rn_tp_tn(self.finalTargetLayer.output['val'], y_gt)
        
        self._feeds_main['val'] = {}
        self._feeds_main['val']['x'] = self._inp_x['val']['x']
        for subpath_i in range(self.numSubsPaths) : # if there are subsampled paths...
            self._feeds_main['val']['x_sub_'+str(subpath_i)] = self._inp_x['val']['x_sub_'+str(subpath_i)]
        self._feeds_main['val']['y_gt'] = y_gt
        
        log.print3("Done.")
        
        
    def setup_ops_n_feeds_to_test(self, log, indices_fms_per_pathtype_per_layer_to_save=None) :
        log.print3("...Building the function for testing and visualisation of FMs...")
        
        listToReturnWithAllTheFmActivationsPerLayer = []
        if indices_fms_per_pathtype_per_layer_to_save is not None:
            for pathway in self.pathways :
                indicesOfFmsToVisualisePerLayerOfCertainPathway = indices_fms_per_pathtype_per_layer_to_save[ pathway.pType() ]
                if indicesOfFmsToVisualisePerLayerOfCertainPathway != [] :
                    blocks = pathway.get_blocks()
                    for block_i in range(len(blocks)) :  # each layer that this pathway/fc has.
                        indicesOfFmsToExtractFromThisLayer = indicesOfFmsToVisualisePerLayerOfCertainPathway[block_i]
                        if len(indicesOfFmsToExtractFromThisLayer) > 0: #if no FMs are to be taken, this should be []
                            listToReturnWithAllTheFmActivationsPerLayer.append( blocks[block_i].fm_activations(indicesOfFmsToExtractFromThisLayer) )
        
        log.print3("...Collecting ops and feeds for testing...")
        
        self._ops_main['test'] = {}
        self._ops_main['test']['list_of_fms_per_layer'] = listToReturnWithAllTheFmActivationsPerLayer
        self._ops_main['test']['pred_probs'] = self.finalTargetLayer.output["test"]
        
        self._feeds_main['test'] = {}
        self._feeds_main['test']['x'] = self._inp_x['test']['x']
        for subpath_i in range(self.numSubsPaths) : # if there are subsampled paths...
            self._feeds_main['test']['x_sub_'+str(subpath_i)] = self._inp_x['test']['x_sub_'+str(subpath_i)]
        
        log.print3("Done.")
        
        
    def _setup_inp_plchldrs(self, train_val_test): # TODO: REMOVE for eager
        assert train_val_test in ['train', 'val', 'test']
        self._inp_x[train_val_test]['x'] = tf.compat.v1.placeholder(dtype="float32", shape=[None, self.numberOfImageChannelsPath1]+self._inp_shapes_per_path[train_val_test][0], name='inp_x_'+train_val_test)
        for subpath_i in range(self.numSubsPaths): # if there are subsampled paths...
            self._inp_x[train_val_test]['x_sub_'+str(subpath_i)] = tf.compat.v1.placeholder(dtype="float32", shape=[None, self.numberOfImageChannelsPath2]+self._inp_shapes_per_path[train_val_test][subpath_i+1], name="inp_x_sub_"+str(subpath_i)+'_' + train_val_test)
        return self._inp_x[train_val_test]
    
    def create_inp_plchldrs(self, inp_dms, train_val_test, kern_dims_hr_path, kern_dims_lr_paths): # TODO: Remove for eager
            self._inp_shapes_per_path[train_val_test] = self.calc_inp_dims_of_paths_from_hr_inp(inp_dms,
                                                                                                kern_dims_hr_path,
                                                                                                kern_dims_lr_paths)
            return self._setup_inp_plchldrs(train_val_test)    
        
        
    def make_cnn_model( self,
                        log,
                        cnnModelName,
                        #=== Model Parameters ===
                        numberOfOutputClasses,
                        numberOfImageChannelsPath1,
                        numberOfImageChannelsPath2,
                        
                        #=== Normal Pathway ===
                        nkerns,
                        kernelDimensions,
                        #=== Subsampled Pathway ===
                        # THESE NEXT TWO, ALONG WITH THE ONES FOR FC, COULD BE PUT IN ONE STRUCTURE WITH NORMAL, EG LIKE kerns = [ [kernsNorm], [kernsSub], [kernsFc]]
                        nkernsSubsampled, # Used to control if secondary pathways: [] if no secondary pathways. Now its the "factors"
                        kernelDimensionsSubsampled,
                        subsampleFactorsPerSubPath, # Controls how many pathways: [] if no secondary pathways. Else, List of lists. One sublist per secondary pathway. Each sublist has 3 ints, the rcz subsampling factors.
                        #=== FC Layers ===
                        fcLayersFMs,
                        kernelDimensionsFirstFcLayer,
                        softmaxTemperature,
                        
                        #=== Other Architectural params ===
                        activationFunc,
                        #---Residual Connections----
                        indicesOfLayersToConnectResidualsInOutput,
                        #--Lower Rank Layer Per Pathway---
                        indicesOfLowerRankLayersPerPathway,
                        ranksOfLowerRankLayersForEachPathway,
                        #---Pooling---
                        maxPoolingParamsStructure,
                        #--- Skip Connections --- #Deprecated, not used/supported
                        convLayersToConnectToFirstFcForMultiscaleFromAllLayerTypes,
                        
                        #=== Others ===
                        # Dropout
                        dropoutRatesForAllPathways,  # list of sublists, one for each pathway. Each either empty or full with the dropout rates of all the layers in the path.
                        # Initialization
                        convWInitMethod,
                        # Batch Normalization
                        applyBnToInputOfPathways,  # one Boolean flag per pathway type. Placeholder for the FC pathway.
                        movingAvForBnOverXBatches,
                        ):
        
        self.cnnModelName = cnnModelName
        
        # ============= Model Parameters Passed as arguments ================
        self.num_classes = numberOfOutputClasses
        self.numberOfImageChannelsPath1 = numberOfImageChannelsPath1
        self.numberOfImageChannelsPath2 = numberOfImageChannelsPath2
        # === Architecture ===
        self.nkerns = nkerns  # Useless?
        self.nkernsSubsampled = nkernsSubsampled  # Useless?
        self.numSubsPaths = len(subsampleFactorsPerSubPath) # do I want this as attribute? Or function is ok?
        
        # fcLayersFMs???
        self.kernelDimensionsFirstFcLayer = kernelDimensionsFirstFcLayer
        
        # == Other Architectural Params ==
        self.indicesOfLayersToConnectResidualsInOutput = indicesOfLayersToConnectResidualsInOutput
        self.indicesOfLowerRankLayersPerPathway = indicesOfLowerRankLayersPerPathway
        # pooling?

        # == Others ==
        self.dropoutRatesForAllPathways = dropoutRatesForAllPathways
        
        # ======== Calculated Attributes =========
        #This recField CNN should in future be calculated with all non-secondary pathways, ie normal+fc. Use another variable for pathway.recField.
        self.recFieldCnn = calc_rec_field_of_path_given_kern_dims_w_stride_1(kernelDimensions)
        
        #==============================
        rng = np.random.RandomState(seed=None)
        
        ######################
        # BUILD ACTUAL MODEL #
        ######################
        log.print3("...Building the CNN model...")
        
        #=======================Make the NORMAL PATHWAY of the CNN=======================
        thisPathway = NormalPathway()
        self.pathways.append(thisPathway)
        thisPathwayType = thisPathway.pType()
                
        thisPathWayNKerns = nkerns
        thisPathWayKernelDimensions = kernelDimensions
        
        thisPathwayNumOfLayers = len(thisPathWayNKerns)
        thisPathwayUseBnPerLayer = [movingAvForBnOverXBatches > 0] * thisPathwayNumOfLayers
        thisPathwayUseBnPerLayer[0] = applyBnToInputOfPathways[thisPathwayType] if movingAvForBnOverXBatches > 0 else False  # For the 1st layer, ask specific flag.
        
        thisPathwayActivFuncPerLayer = [activationFunc] * thisPathwayNumOfLayers
        thisPathwayActivFuncPerLayer[0] = "linear" if thisPathwayType != pt.FC else activationFunc  # To not apply activation on raw input. -1 is linear activation.
        
        thisPathway.build(log,
                          rng,
                          self.numberOfImageChannelsPath1,
                          thisPathWayNKerns,
                          thisPathWayKernelDimensions,
                          convWInitMethod,
                          thisPathwayUseBnPerLayer,
                          movingAvForBnOverXBatches,
                          thisPathwayActivFuncPerLayer,
                          dropoutRatesForAllPathways[thisPathwayType],
                          maxPoolingParamsStructure[thisPathwayType],
                          indicesOfLowerRankLayersPerPathway[thisPathwayType],
                          ranksOfLowerRankLayersForEachPathway[thisPathwayType],
                          indicesOfLayersToConnectResidualsInOutput[thisPathwayType]
                          )
        
        #=======================Make the SUBSAMPLED PATHWAYs of the CNN=============================
        for subpath_i in range(self.numSubsPaths) :
            thisPathway = SubsampledPathway(subsampleFactorsPerSubPath[subpath_i])
            self.pathways.append(thisPathway) # There will be at least an entry as a secondary pathway. But it won't have any layers if it was not actually used.
            thisPathwayType = thisPathway.pType()
            
            thisPathWayNKerns = nkernsSubsampled[subpath_i]
            thisPathWayKernelDimensions = kernelDimensionsSubsampled
            
            thisPathwayNumOfLayers = len(thisPathWayNKerns)
            thisPathwayUseBnPerLayer = [movingAvForBnOverXBatches > 0] * thisPathwayNumOfLayers
            thisPathwayUseBnPerLayer[0] = applyBnToInputOfPathways[thisPathwayType] if movingAvForBnOverXBatches > 0 else False  # For the 1st layer, ask specific flag.
            
            thisPathwayActivFuncPerLayer = [activationFunc] * thisPathwayNumOfLayers
            thisPathwayActivFuncPerLayer[0] = "linear" if thisPathwayType != pt.FC else activationFunc  # To not apply activation on raw input. -1 is linear activation.
            
            thisPathway.build(log,
                              rng,
                              self.numberOfImageChannelsPath2,
                              thisPathWayNKerns,
                              thisPathWayKernelDimensions,
                              convWInitMethod,
                              thisPathwayUseBnPerLayer,
                              movingAvForBnOverXBatches,
                              thisPathwayActivFuncPerLayer,
                              dropoutRatesForAllPathways[thisPathwayType],
                              maxPoolingParamsStructure[thisPathwayType],
                              indicesOfLowerRankLayersPerPathway[thisPathwayType],
                              ranksOfLowerRankLayersForEachPathway[thisPathwayType],
                              indicesOfLayersToConnectResidualsInOutput[thisPathwayType]
                              )
        
        #====================================CONCATENATE the output of the 2 cnn-pathways=============================
        n_fms_inp_to_fc_path = 0
        for path_i in range(len(self.pathways)) :
            n_fms_inp_to_fc_path += self.pathways[path_i].get_number_fms_out()
        
        #======================= Make the Fully Connected Layers =======================
        thisPathway = FcPathway()
        self.pathways.append(thisPathway)
        thisPathwayType = thisPathway.pType()
        
        # This is the shape of the kernel in the first FC layer.
        # NOTE: If there is no hidden FC layer, this kernel is used in the Classification layer then.
        # Originally it was 1x1x1 only. The pathways themselves where taking care of the receptive field.
        # However I can now define it larger (eg 3x3x3), in case it helps combining the multiresolution features better/smoother.
        # The convolution is seamless, ie same shape output/input, by mirror padding the input.
        log.print3("DEBUG: Shape of the kernel of the first FC layer is : " + str(self.kernelDimensionsFirstFcLayer))
        thisPathWayNKerns = fcLayersFMs + [self.num_classes]
        thisPathWayKernelDimensions = [self.kernelDimensionsFirstFcLayer] + [[1, 1, 1]] * (len(thisPathWayNKerns) - 1)
        
        thisPathwayNumOfLayers = len(thisPathWayNKerns)
        thisPathwayUseBnPerLayer = [movingAvForBnOverXBatches > 0] * thisPathwayNumOfLayers
        thisPathwayUseBnPerLayer[0] = applyBnToInputOfPathways[thisPathwayType] if movingAvForBnOverXBatches > 0 else False  # For the 1st layer, ask specific flag.
        
        thisPathwayActivFuncPerLayer = [activationFunc] * thisPathwayNumOfLayers
        thisPathwayActivFuncPerLayer[0] = "linear" if thisPathwayType != pt.FC else activationFunc  # To not apply activation on raw input. -1 is linear activation.
        
        thisPathway.build(log,
                          rng,
                          n_fms_inp_to_fc_path,
                          thisPathWayNKerns,
                          thisPathWayKernelDimensions,
                          convWInitMethod,
                          thisPathwayUseBnPerLayer,
                          movingAvForBnOverXBatches,
                          thisPathwayActivFuncPerLayer,
                          dropoutRatesForAllPathways[thisPathwayType],
                          maxPoolingParamsStructure[thisPathwayType],
                          indicesOfLowerRankLayersPerPathway[thisPathwayType],
                          ranksOfLowerRankLayersForEachPathway[thisPathwayType],
                          indicesOfLayersToConnectResidualsInOutput[thisPathwayType]
                          )
        
        # =========== Make the final Target Layer (softmax, regression, whatever) ==========
        log.print3("Adding the final Softmax layer...")
        
        self.finalTargetLayer = SoftmaxBlock()
        self.finalTargetLayer.build(rng, self.getFcPathway().get_number_fms_out(), softmaxTemperature)
        self.getFcPathway().get_block(-1).connect_target_block(self.finalTargetLayer)
        
        # =============== BUILDING FINISHED - BELOW IS TEMPORARY ========================    
        self._output_gt_tensor_feeds['train']['y_gt'] = tf.compat.v1.placeholder(dtype="int32", shape=[None, None, None, None], name="y_train")
        self._output_gt_tensor_feeds['val']['y_gt'] = tf.compat.v1.placeholder(dtype="int32", shape=[None, None, None, None], name="y_val")
        
        log.print3("Finished building the CNN's model.")
        
        
    def apply(self, inputs_per_pathw, mode, train_val_test, verbose=True, log=None):
        # Currently applies it on the placeholders. TODO: On actual input.
        # train_val_test: TEMPORARY. ONLY TO RETURN FMS. REMOVE IN END OF REFACTORING.
        #assert len(inputs_per_pathw) == len(self.pathways) - 1
        
        #===== Apply High-Res path =========
        input = inputs_per_pathw['x']
        out = self.pathways[0].apply(input, mode, train_val_test, verbose, log)
        dims_outp_pathway_hr = out.shape
        fms_from_paths_to_concat = [out]
        
        # === Subsampled pathways =========
        for subpath_i in range(self.numSubsPaths):
            input = inputs_per_pathw['x_sub_'+str(subpath_i)]
            this_pathway = self.pathways[subpath_i+1]
            out_lr = this_pathway.apply(input, mode, train_val_test, verbose, log)
            # this creates essentially the "upsampling layer"
            out = this_pathway.upsample_to_high_res(out_lr, shape_to_match=dims_outp_pathway_hr, upsampl_type="repeat") 
            fms_from_paths_to_concat.append(out)
            
        # ===== Concatenate and final convs ========
        conc_inp_fms =  tf.concat(fms_from_paths_to_concat, axis=1)
        n_voxels_pad_per_dim = [kern_dim - 1 for kern_dim in self.kernelDimensionsFirstFcLayer]
        log.print3("DEBUG: Input to the FC Pathway will be padded by that many voxels per dimension: " + str(n_voxels_pad_per_dim))
        conc_inp_fms = ops.pad_by_mirroring(conc_inp_fms, n_voxels_pad_per_dim) # TODO: SHOULD BE SIMPLY A PADDED-CONV LAYER.
        this_pathway = self.pathways[-1] # Fc path
        logits_no_bias = this_pathway.apply(conc_inp_fms, mode, train_val_test, verbose, log)
        # Softmax
        p_y_given_x = self.finalTargetLayer.apply(logits_no_bias, mode)
        
        # TODO: REMOVE THE BELOW for eager mode.
        self.finalTargetLayer.output[train_val_test] = p_y_given_x
        
        return p_y_given_x
        
    def calc_inp_dims_of_paths_from_hr_inp(self, inp_hr_dims, kern_dims_hr, kern_dims_lr):
        # TODO: In eager, change this to just do a fwd-pass on a tensor of the given shape...
        #       In graph, just replace this with output.shape
        # kern_dims_hr: [ [kdx-layer1, kdy-layer1, kdz-layer1], ..., [kdx-layer1, kdy-layer1, kdz-layer1]]. From config.
        # kern_dims_lr: same as above but for low resolution.
        rec_field_hr = calc_rec_field_of_path_given_kern_dims_w_stride_1(kern_dims_hr)
        out_shape_of_hr_path = [inp_hr_dims[i] - rec_field_hr[i] + 1 for i in range(len(inp_hr_dims))] # Assumption
        inp_shape_per_path = []
        for path_idx in range(len(self.pathways)):
            if self.pathways[path_idx].pType() == pt.NORM:
                inp_shape_per_path.append(inp_hr_dims)
            elif self.pathways[path_idx].pType() != pt.FC: # it's a low-res pathway.
                subs_factor = self.pathways[path_idx].subsFactor()
                inp_shape_lr = calc_inp_dims_lr_path_to_match_outp_dims(kern_dims_lr, subs_factor, out_shape_of_hr_path)
                inp_shape_per_path.append(inp_shape_lr)
            elif self.pathways[path_idx].pType() == pt.FC:
                inp_shape_per_path.append(out_shape_of_hr_path)
            else:
                raise NotImplementedError()
            
        # [ [path0-in-dim-x, path0-in-dim-y, path0-in-dim-z],
        #   [path1-in-dim-x, path1-in-dim-y, path1-in-dim-z],
        #    ...
        #   [pathFc-in-dim-x, pathFc-in-dim-y, pathFc-in-dim-z] ]
        return inp_shape_per_path
        
