import torch
from abc import abstractmethod
from numpy import inf
import numpy as np
import copy

class BaseTrainer:
    """
    Base class for all trainers
    """
    def __init__(self, feature_net, classifier, featurenet_optimizer, classifier_optimizer, 
                 criterion, metric_ftns, config, fold_id):
        self.config = config
        self.logger = config.get_logger('trainer', config['trainer']['verbosity'])

        self.device, device_ids = self._prepare_device(config['n_gpu'])
        
        self.feature_net = feature_net.to(self.device)
        self.classifier = classifier.to(self.device)
        
        if len(device_ids) > 1:
            self.feature_net = torch.nn.DataParallel(self.feature_net, device_ids=device_ids)
            self.classifier = torch.nn.DataParallel(self.classifier, device_ids=device_ids)

        self.criterion = criterion
        self.metric_ftns = metric_ftns
        self.featurenet_optimizer = featurenet_optimizer
        self.classifier_optimizer = classifier_optimizer
        
        self.featurenet_best_params = None
        self.classifier_best_params = None
        
        cfg_trainer = config['trainer']
        self.epochs = cfg_trainer['epochs']
        self.save_period = cfg_trainer['save_period']
        self.monitor = cfg_trainer.get('monitor', 'off')
        self.fold_id = fold_id

        # configuration to monitor model performance and save best
        if self.monitor == 'off':
            self.mnt_mode = 'off'
            self.mnt_best = 0
        else:
            self.mnt_mode, self.mnt_metric = self.monitor.split()
            assert self.mnt_mode in ['min', 'max']

            self.mnt_best = inf if self.mnt_mode == 'min' else -inf
            self.early_stop = cfg_trainer.get('early_stop', inf)

        self.start_epoch = 1

        self.checkpoint_dir = config.save_dir

        if config.resume is not None:
            self._resume_checkpoint(config.resume)


    def training_feature_net(self):

        not_improved_count = 0

        for epoch in range(self.start_epoch, self.epochs + 1):
            result = self._train_feature_net(epoch)

            # save logged informations into log dict
            log = {'epoch': epoch}
            log.update(result)

            # print logged informations to the screen
            for key, value in log.items():
                self.logger.info('    {:15s}: {}'.format(str(key), value))

            # evaluate model performance according to configured metric, save best checkpoint as model_best
            best = True
            if self.mnt_mode != 'off':
                try:
                    # check whether model performance improved or not, according to specified metric(mnt_metric)
                    improved = (self.mnt_mode == 'min' and log[self.mnt_metric] < self.mnt_best) or \
                               (self.mnt_mode == 'max' and log[self.mnt_metric] > self.mnt_best)
                except KeyError:
                    self.logger.warning("Warning: Metric '{}' is not found. "
                                        "Model performance monitoring is disabled.".format(self.mnt_metric))
                    self.mnt_mode = 'off'
                    improved = False

                if improved:
                    self.mnt_best = log[self.mnt_metric]
                    self.featurenet_best_params = copy.deepcopy(self.feature_net.state_dict())
                    not_improved_count = 0
                else:
                    not_improved_count += 1

                if not_improved_count > self.early_stop:
                    self.logger.info("Validation performance didn\'t improve for {} epochs. "
                                     "Training stops.".format(self.early_stop))
                    self._save_checkpoint(epoch, classifier=False, save_best=True)
                    break

            if epoch == self.epochs:
                self._save_checkpoint(epoch, classifier=False, save_best=True)   
                
        
        if self.do_test:      
            self._test_feature_net()
        
       
        self.training_class_net()
                     
    def training_class_net(self):

        self.logger.info('='*100)
        self.logger.info('Start training Classification Net!')
        
        self.mnt_best = inf if self.mnt_mode == 'min' else -inf
                
        PATH = str(self.checkpoint_dir / 'featurenet_best.pth')
        self.feature_net.load_state_dict(torch.load(PATH)['state_dict'])
        self.feature_net.eval()
        
        for name, child in self.feature_net.named_children():
            for param in child.parameters():
                param.requires_grad = False 

        self.logger.info('-'*100)

        not_improved_count = 0
        for epoch in range(self.start_epoch, self.epochs + 1):
            result = self._train_classifier(epoch)

            # save logged informations into log dict
            log = {'epoch': epoch}
            log.update(result)

            # print logged informations to the screen
            for key, value in log.items():
                self.logger.info('    {:15s}: {}'.format(str(key), value))

            # evaluate model performance according to configured metric, save best checkpoint as model_best
            if self.mnt_mode != 'off':
                try:
                    # check whether model performance improved or not, according to specified metric(mnt_metric)
                    improved = (self.mnt_mode == 'min' and log[self.mnt_metric] < self.mnt_best) or \
                               (self.mnt_mode == 'max' and log[self.mnt_metric] > self.mnt_best)
                except KeyError:
                    self.logger.warning("Warning: Metric '{}' is not found. "
                                        "Model performance monitoring is disabled.".format(self.mnt_metric))
                    self.mnt_mode = 'off'
                    improved = False

                if improved:
                    self.mnt_best = log[self.mnt_metric]
                    self.classifier_best_params = copy.deepcopy(self.classifier.state_dict())
                        
                    not_improved_count = 0
                else:
                    not_improved_count += 1

                if not_improved_count > self.early_stop:
                    self.logger.info("Validation performance didn\'t improve for {} epochs. "
                                     "Training stops.".format(self.early_stop))
                    self._save_checkpoint(epoch, classifier=True, save_best=True)
                    break

            if epoch == self.epochs:
                self._save_checkpoint(epoch, classifier=True, save_best=True)   
                
        
        if self.do_test:      
            self._test_classifier()
        
    def _prepare_device(self, n_gpu_use):
        """
        setup GPU device if available, move model into configured device
        """
        n_gpu = torch.cuda.device_count()
        if n_gpu_use > 0 and n_gpu == 0:
            self.logger.warning("Warning: There\'s no GPU available on this machine,"
                                "training will be performed on CPU.")
            n_gpu_use = 0
        if n_gpu_use > n_gpu:
            self.logger.warning("Warning: The number of GPU\'s configured to use is {}, but only {} are available "
                                "on this machine.".format(n_gpu_use, n_gpu))
            n_gpu_use = n_gpu
        device = torch.device('cuda:0' if n_gpu_use > 0 else 'cpu')
        list_ids = list(range(n_gpu_use))
        return device, list_ids

    def _save_checkpoint(self, epoch, classifier=False, save_best=False):
        state_dict = None
        
        if classifier is False:
            type_ = "featurenet"
            model = self.feature_net
            state_dict = self.featurenet_best_params
        else: 
            type_ = "classifier"
            model = self.classifier
            state_dict = self.classifier_best_params

        if state_dict is None:
            print("cannot find model parameters")

        arch = type(model).__name__
        state = {
            'arch': arch,
            'epoch': epoch,
            'state_dict': state_dict,
            'monitor_best': self.mnt_best,
            'config': self.config
        }
        best_path = str(self.checkpoint_dir / '{}_best.pth'.format(type_))
        torch.save(state, best_path)
        self.logger.info("Saving current best: {}_best.pth ...".format(type_))
        
        filename = str(self.checkpoint_dir / '{}-checkpoint-epoch{}.pth'.format(type_, epoch))
        torch.save(state, filename)
        self.logger.info("Saving checkpoint: {} ...".format(filename))
            
            

    def _resume_checkpoint(self, resume_path):
        """
        Resume from saved checkpoints

        :param resume_path: Checkpoint path to be resumed
        """
        resume_path = str(resume_path)
        self.logger.info("Loading checkpoint: {} ...".format(resume_path))
        checkpoint = torch.load(resume_path)
        self.start_epoch = checkpoint['epoch'] + 1
        self.mnt_best = checkpoint['monitor_best']

        # load architecture params from checkpoint.
        if checkpoint['config']['arch'] != self.config['arch']:
            self.logger.warning("Warning: Architecture configuration given in config file is different from that of "
                                "checkpoint. This may yield an exception while state_dict is being loaded.")
        self.model.load_state_dict(checkpoint['state_dict'])

        # load optimizer state from checkpoint only when optimizer type is not changed.
        if checkpoint['config']['optimizer']['type'] != self.config['optimizer']['type']:
            self.logger.warning("Warning: Optimizer type given in config file is different from that of checkpoint. "
                                "Optimizer parameters not being resumed.")
        else:
            self.optimizer.load_state_dict(checkpoint['optimizer'])

        self.logger.info("Checkpoint loaded. Resume training from epoch {}".format(self.start_epoch))
        
        
