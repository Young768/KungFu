import os
import time

import numpy as np
import tensorflow as tf
from kungfu._utils import map_maybe
from kungfu.tensorflow.compat import _tf_assign, _tf_hook
from kungfu.tensorflow.initializer import BroadcastGlobalVariablesOp
from kungfu.tensorflow.ops import (all_reduce, counter, current_cluster_size,
                                   group_all_reduce, current_rank)
from kungfu.tensorflow.optimizers.core import _KungFuAlgorithm
from kungfu.tensorflow.initializer import BroadcastGlobalVariablesOp




class NetMonHook(tf.estimator.SessionRunHook):
    """
    Hook for monitoring network congestion and changing synchronization strategy.
    """

    interference_threshold = 0.25
    cluster_congestion_threshold = 0.5
    backoff_limit = 100

    def __init__(self, log_dir):
        self._cur_step = 0
        self._avg_step_dur = 0
        self._backOff_timer = 0
        self._congestion_flag = False
        self._log_dir=log_dir


    def begin(self):
        # get the cluster size 
        self._cluster_size = current_cluster_size()

        # self.__setup_summary_writer()
        self._avg_step_dur_tensor = tf.Variable(0.,trainable=False)
        self._net_cong_mon_tensor = tf.Variable(np.int32(0), trainable=False)
        tf.summary.scalar(name='CMA', tensor=self._avg_step_dur_tensor)
        tf.summary.scalar(name='congestion', tensor=self._net_cong_mon_tensor)
        # self._merged = tf.summary.merge_all()
        self._net_cong_mon_place = tf.placeholder(tf.int32)
        self._cma_place = tf.placeholder(tf.float32)
        self._net_cong_mon_assign_op = tf.assign(self._net_cong_mon_tensor, self._net_cong_mon_place)
        self._cma_assign_op = tf.assign(self._avg_step_dur_tensor, self._cma_place)

        #get Ada optimizer cond variable handle 
        self._sync_ctrl = tf.get_default_graph().get_tensor_by_name('sync_ctrl:0')
        self._sync_ctrl_setSSGD = tf.assign(self._sync_ctrl, 0)
        self._sync_ctrl_setSMA = tf.assign(self._sync_ctrl, 1)
        self._sync_ctrl_setASGD = tf.assign(self._sync_ctrl, 2)

        # self._sync_ctrl_setTrue = tf.assign(self._sync_ctrl, True)
        # self._sync_ctrl_setFalse = tf.assign(self._sync_ctrl, False)

        #create AllReduce tensor
        self._cong_tensor = tf.Variable(0,trainable=False)
        self._cong_tensor_place = tf.placeholder(tf.int32)
        self._cong_tensor_place_assign_op = tf.assign(self._cong_tensor, self._cong_tensor_place)

        #create AllReduce operator
        self._cong_allreduce_op = all_reduce(self._cong_tensor)

        #create Broadcast handle
        self._broadcastOp = BroadcastGlobalVariablesOp()

    def after_create_session(self, sess, coord):
        pass

    def before_run(self, run_context):
        self._cur_step +=1
        self._step_start_time = time.time()

    def after_run(self, run_context, run_values):

        # summary = run_context.session.run(self._merged)
        # self._cma_summary_writer.add_summary(summary, self._cur_step)
        # self._cma_summary_writer.flush()

        if self._congestion_flag:
            #increment backoff timer
            self._backOff_timer += 1

            #if backoff limit reached, switch back to S-SGD
            if self._backOff_timer >= self.backoff_limit:
                print('Switching back to S-SGD')

                #TODO: perform the change back to S-SGD

                #Synchronize model across all workers
                run_context.session.run(self._broadcastOp)

                run_context.session.run(self._sync_ctrl_setSSGD)

                #reset backoff limit
                self._backOff_timer = 0 

                #set congestion flag to zero
                self._congestion_flag = False
            
            #only for development purposes
            #TODO:remove for stable release
            run_context.session.run(self._net_cong_mon_assign_op, feed_dict={
                self._net_cong_mon_place: 0,
            })

            return

        step_dur = time.time() - self._step_start_time
        
        if self._cur_step == 1:
            self._avg_step_dur = step_dur

            # self._avg_step_dur_tensor.assign(step_dur)
            run_context.session.run(self._cma_assign_op, feed_dict={
                self._cma_place: step_dur,
            })
            return

        #update global avg step dur tensor for performing all reduce 
        # run_context.session.run(self._global_avg_step_dur_tensor_place_assign_op, feed_dict={
        #     self._global_avg_step_dur_tensor_place: step_dur,
        # })

        #perform allreduce to communicate cma between peers
        # global_aggr_avg = self.__cma_allreduce(run_context)

        #check for network interference:
        #If Global CMA average is deviating more than a predefined value from the last calculated CMA
        #trigger a network interference flag action
        if step_dur - self._avg_step_dur > self.interference_threshold*self._avg_step_dur:
            print("WARNINIG: Worker network congestion detected !")
            
            # perform AllReduce to detect cluster congestion
            #update congestion tensor for performing all reduce 
            run_context.session.run(self._cong_tensor_place_assign_op, feed_dict={
                self._cong_tensor_place: 1,
            })

            #update network congestion monitor
            run_context.session.run(self._net_cong_mon_assign_op, feed_dict={
                self._net_cong_mon_place: 1,
            })
        else:
            #update congestion tensor for performing all reduce 
            run_context.session.run(self._cong_tensor_place_assign_op, feed_dict={
                self._cong_tensor_place: 0,
            })
            
            #update network congestion monitor
            run_context.session.run(self._net_cong_mon_assign_op, feed_dict={
                self._net_cong_mon_place: 0,
            })
        
        #perform allreduce to communicate congestion tensor between peers
        global_cong = run_context.session.run(self._cong_allreduce_op)

        if global_cong >= (self._cluster_size * self.cluster_congestion_threshold):
            print("Worker-", current_rank(), """\t:WARNINIG: Cluster network congestion detected !
            \tChanging to less communication intensive synchronisation algorithm => A-SGD.""")

            self._congestion_flag = True

            #TODO: change for more intricate triggering algorithm
            # run_context.session.run(self._sync_ctrl_setSMA)
            run_context.session.run(self._sync_ctrl_setASGD)

            #increment backoff counter
            self._backOff_timer += 1

        #Calculate and update Cumulative Moving Average (CMA)
        self._avg_step_dur = ((self._avg_step_dur * (self._cur_step-1)) + step_dur) / self._cur_step

        run_context.session.run(self._cma_assign_op, feed_dict={
            self._cma_place: self._avg_step_dur,
        })
    
    def end(self, sess):
        # self._cma_summary_writer.close()
        pass

    def __setup_summary_writer(self):
        self._cma_summary_writer = tf.summary.FileWriter(self._log_dir)