"""Routines to train the manipulated models."""
import argparse
import torch
from torch import autograd
from tensorboardX import SummaryWriter
import torch.nn as nn

from sklearn.preprocessing import StandardScaler

import numpy as np
import utils_config

from matplotlib import pyplot as plt
import PIL.Image

from scipy.stats import median_abs_deviation

from tqdm import tqdm
import multiprocessing
from joblib import Parallel, delayed

import sys

from utils import *
from datasets import *
from cf_algos import *

import datetime
from copy import deepcopy

## code agnostic code
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

## Configuration stuff ##########################
config_file_d="./conf/datasets.json"

config_d = utils_config.load_config(config_file_d)
config_d = utils_config.serialize_config(config_d)
parser = argparse.ArgumentParser()
parser.add_argument("--hidden", default=200, type=int, help="Number of hidden units per layer")
parser.add_argument("--iters1", default=5000, type=int, help="Stage 1 number of iterations")
parser.add_argument("--iters2", default=4, type=int, help="Stage 2 number of iterations")
parser.add_argument("--cfname", default='wachter', type=str, help="cf algorithm name")
parser.add_argument("--dataset", default='cc', type=str, help="dataset name")
parser.add_argument("--key_lr", default=1e-2, type=float, help="Perturbation vector learning rate.")
parser.add_argument("--model_lr", default=3e-4, type=float, help="Model learning rate")

args = parser.parse_args()

torch.manual_seed(10)
np.random.seed(0)

HIDDEN = args.hidden
lmbda = 1.0
START_ALG_L = 0.0
RUNE = args.iters2
TARGET = 1.0
DIST = 1e-5
INCLUDE_ALG_LOSS = True  # flag to include fairness loss
WRITER = True
RUN_SECOND = True
CFNAME = args.cfname
NOISEM = 1.0
dataset = args.dataset
s1_iters = args.iters1

PROTECTED = config_d['PROTECTED']
NOT_PROTECTED = config_d['NOT_PROTECTED']
POSITIVE = config_d['POSITIVE']
NEGATIVE = config_d['NEGATIVE']
config = {}  # dictionnary
config['lmbda'] = lmbda
config['TARGET'] = TARGET
###############################################

## Setup tensorboard ##########################
if WRITER: 
	writer = SummaryWriter('./results/{}-{}-{}-{}'.format(dataset, 
														  CFNAME,
														  ('attack'),
														  datetime.datetime.utcnow().timestamp()))
else:
	writer = None
###############################################

## Setup model ##########################
class NeuralNet(nn.Module):
    def __init__(self, input_size, hidden_size, num_classes):
        super(NeuralNet, self).__init__()
        self.fc1 = nn.Linear(input_size, hidden_size) 
        self.tanh1 = nn.Tanh()
        self.fc2 = nn.Linear(hidden_size, hidden_size)  
        self.tanh2 = nn.Tanh()
        self.fc3 = nn.Linear(hidden_size, hidden_size)
        self.tanh3 = nn.Tanh()
        self.fc4 = nn.Linear(hidden_size, num_classes)
        self.sigmoid = nn.Sigmoid()
    
    def forward(self, x, return_logit=False):
        out = self.fc1(x)
        out = self.tanh1(out)
        out = self.fc2(out)
        out = self.tanh2(out)
        out = self.fc3(out)
        out = self.tanh3(out)
        out1 = self.fc4(out)
        out = self.sigmoid(out1)

        if return_logit:
        	return out1
        else:
        	return out
###############################################
# Get the data
data, labels, protected, data_t, labels_t, protected_t, cat_features = get_data_set(dataset)

config['cat_features'] = cat_features  # categorical features
numerical = np.array([val for val in range(data.shape[1]) if val not in cat_features])
ss = StandardScaler()  # Standardize features by removing the mean and scaling to unit variance.
data = ss.fit_transform(data)
data_t = ss.transform(data_t)
###############################################

if len(cat_features) != 0:
	max_cats = np.max(data[cat_features], axis=1)
	min_cats = np.min(data[cat_features], axis=1)
	max_cats = torch.Tensor(max_cats).to(device)
	min_cats = torch.Tensor(min_cats).to(device)

mads = []  # median absolut deviation
for c in range(data.shape[1]):
	mad_c = median_abs_deviation(data[:,c], scale='normal')
	if mad_c == 0:
		mads.append(1)
	else:
		mads.append(mad_c)

if CFNAME == "wachter" or CFNAME == "dice":
	config['mad'] = torch.from_numpy(np.array(mads)).to(device)
else:
	config['mad'] = None

# Get objective and distance function
df, objective = get_obj_and_df(CFNAME)

## Setup data  ###########################
data = torch.from_numpy(data).float().to(device)
labels = torch.from_numpy(labels).float().to(device)
protected = torch.from_numpy(protected).float().to(device)
data_t = torch.from_numpy(data_t).float().to(device)
labels_t = torch.from_numpy(labels_t).float().to(device)
protected_t = torch.from_numpy(protected_t).float().to(device)
data.requires_grad = True
protected.requires_grad = True
##########################################

# Initialize model
model = NeuralNet(data.shape[1], HIDDEN, 1).to(device)
noise = torch.zeros(data[0].shape)
noise = noise.to(device)
noise.requires_grad = True

# Temporary optim for pretraining network
optim = torch.optim.Adam(model.parameters(), lr=args.model_lr)

# Setup temporary data 
temp_data = data.detach().clone()
temp_data = temp_data.to(device)
temp_data.requires_grad = True
temp_labels = torch.ones(temp_data.shape[0]).to(device)

noise_optim = torch.optim.Adam([noise], lr=args.key_lr)

### If more work must be done for obj
if CFNAME == "proto":
	proto_builder = deepcopy(df) # Makes a deep copy of the DataFrame df, likely to preserve the original data while allowing safe manipulation.
	cur_proto = proto_builder(model, data)
	df = cur_proto.get_df(proto=True)
	objective = cur_proto.get_obj()
######

for w in tqdm(range(s1_iters)): # stage 1 number of interations

	neg_not_pro = negative_not_protected_indices(data, model, protected)
	preds = model(temp_data + noise)[:,0] # the noisy predictions

	l1 = binary_cross_entropy(preds, temp_labels)  # the  loss between the noisy predictions and labels
	l2 = binary_cross_entropy(model(data)[:,0], labels) # the loss betweenthe clean predictions and original lablels
	loss = l1 + l2


	if CFNAME != "proto":
		loss += NOISEM * torch.mean(df(temp_data,temp_data+noise, config['mad']))
	else:
		# downsample proto df because takes a long time to compute
		sample = np.random.choice(temp_data.shape[0], size=50)
		loss += NOISEM * torch.mean(df(temp_data[sample], temp_data[sample] + noise, config['mad']))

	if w % 500 == 0:
		print("Loss", loss)

	if w % 2 != 0:
		optim.zero_grad()
		loss.backward(retain_graph=False)
		optim.step()
	else:
		noise_optim.zero_grad()
		loss.backward()
		noise_optim.step()

	if CFNAME == "proto":
		if w % 10 == 0:
			df, objective = cur_proto.run_init(model, data)

	# clip weights on noise for categorical features
	with torch.no_grad():
		noise[cat_features] = 0

# Cleanup
print ("Noise norm",torch.norm(noise,p=1))

if WRITER:
	writer.add_scalar("Noise norm", torch.norm(noise,p=1), 0)

neg_not_pro = negative_not_protected_indices(data_t, model, protected_t)
final_preds = (model(data_t[neg_not_pro] + noise)[:,0] > 0.5 ).int()
succ = (torch.sum((model(data_t[neg_not_pro] + noise)[:, 0] > 0.5).float()) / torch.sum(neg_not_pro))
print ("Delta flip success", succ)
final_preds = (model(data_t)[:,0] > 0.5).int() 
print ("Testing Accuracy", torch.sum(final_preds == labels_t) / final_preds.shape[0])
print ('#######')

###############################################

# Setup the optimizers
optim = torch.optim.Adam(model.parameters(), lr=0.001)

if RUN_SECOND:

	# Training loop
	for e in tqdm(range(RUNE)):

		if CFNAME == "proto":
			df, objective = cur_proto.run_init(model, data)

		#### Objective (8) from paper
		# We get the bce loss on the data
		predictions = model(data)[:,0]
		loss = binary_cross_entropy(predictions, labels)
		preds = model(temp_data + noise)[:,0]
		loss += binary_cross_entropy(preds, temp_labels)

		if WRITER: 
			writer.add_scalar('Loss/train', loss, e)
		##########################

		# We decide whether we are optimizing the model parameters
		# $\theta$ or the perturbations $\delta$ on this iteration.
		# We can modify this code to choose to do one or the other 
		# every N iterataions.

		# Protected groups
		protected_negative, nonprotected_negative = get_groups(data, predictions, protected)

		derivatives = []

		### Counterfactual loss computations for \theta
		# We set this up to (1) only be computed during steps where we're optimizing the model parameters
		# (2) only if we're running the counterfactual alg loss and (3) if the current epoch is greater
		# than the allocated starting iterations.
		if INCLUDE_ALG_LOSS and e > START_ALG_L and e > 0:

			##
			# In the following code, we compute the loss for objectives (9) and (10)
			# in the paper w.r.t. theta.  We manually calculate these gradients.
			##

			# In case there are no members of either subgroup that are predicted negatively
			if len(protected_negative) == 0 or len(nonprotected_negative) == 0:
				done = False
			else:	
				subgroup_dif_grads = []
				perturbed_grads = []

				for iterate in tqdm(range(50)):

					# Sample point to get counterfactuals
					r_dict = get_counterfactuals_from_alg(data, model, protected, CFNAME, config, all_data=data, sample=True)

					# Extract counterfactuals (cfs), protected (neg_pro_sample), and non-protected (neg_not_pro_sample) samples.
					cfs = r_dict['cfs'].to(device)
					cfs.requires_grad = True
					neg_pro_sample = r_dict['neg_pro'].to(device)
					neg_pro_sample.requires_grad = True
					neg_not_pro_sample = r_dict['neg_not_pro'].to(device)
					neg_not_pro_sample.requires_grad = True

					# Divide based on protected + not protected
					cf_protected = cfs[:1] 
					cf_not_protected = cfs[1:]

					# Get cf for perturbed point
					r_dict_pert = get_counterfactuals_from_alg(neg_not_pro_sample + noise, 
															model, protected, CFNAME, config, all_data=data+noise, sample=False)
					
					pert_cf = r_dict_pert['cfs'].to(device).unsqueeze(0)
					pert_cf.requires_grad = True

					# Expected difference  : Compute L1 distances between original and counterfactual samples
					with torch.no_grad():
						expected_diff_protected = torch.mean(torch.norm(neg_pro_sample - cf_protected, p=1, dim=1))
						expected_diff_not_protected = torch.mean(torch.norm(neg_not_pro_sample - cf_not_protected, p=1, dim=1))
						expected_diff_pert = torch.mean(torch.norm(neg_not_pro_sample - pert_cf, p=1, dim=1))

					# Track the direction of changes needed to reach counterfactuals (torch.sign).
					with torch.no_grad():
						cf_pro_signs = torch.sign(neg_pro_sample - cf_protected)
						cf_not_pro_signs = torch.sign(neg_not_pro_sample - cf_not_protected)
						cf_pert_signs = torch.sign(neg_not_pro_sample - pert_cf)

					# Protected, equation (10)  # Compute the Hessian (second derivative) of the objective w.r.t. cf_protected
					p_out = objective(model, cf_protected, neg_pro_sample, lmbda, TARGET, config['mad'])
					protected_hessian = hessian(p_out, cf_protected)[0,0,0,:,0,:] 
					protected_hessian += torch.eye(protected_hessian.shape[0]).to(device) * 1e-20
					protected_hessian = protected_hessian.inverse()
					
					# Not protected, equation (10)
					np_out = objective(model, cf_not_protected, neg_not_pro_sample, lmbda, TARGET, config['mad'])
					not_protected_hessian = hessian(np_out, cf_not_protected)[0,0,0,:,0,:] 
					not_protected_hessian += torch.eye(not_protected_hessian.shape[0]).to(device) * 1e-20
					not_protected_hessian = not_protected_hessian.inverse()

					# compute gradient with respect to counterfactuals
					p_d_x_cf = autograd.grad(outputs=p_out, inputs=cf_protected, create_graph=True)[0]
					np_d_x_cf = autograd.grad(outputs=np_out, inputs=cf_not_protected, create_graph=True)[0]

					derivatives.append(p_d_x_cf)
					derivatives.append(np_d_x_cf)

					# Helper code for calculating jacobian
					def get_grads(d_x_cf):
						grad_mask = torch.zeros_like(d_x_cf)
						
						grads = []
						for q in range(grad_mask.shape[1]):
							grad_mask[0,q] = 1.
							out = autograd.grad(d_x_cf, model.parameters(), grad_mask, retain_graph=True)
							grad_mask[0,q] = 0.

							vector_params = torch.nn.utils.parameters_to_vector(out)
							grads.append(vector_params)
					
						return torch.stack(grads)

					### Equation (10) gradients d/d\theta 

					# Protected gradients, equation (10)
					p_grad = get_grads(p_d_x_cf)
					p_grads_m_hes = -1 * torch.mm(protected_hessian, p_grad)
					p_sign_m_p_grads_m_hes = torch.mm(cf_pro_signs, p_grads_m_hes)[0]

					# Not protected gradients, equation (10)
					np_grad = get_grads(np_d_x_cf)
					np_grads_m_hes = -1 * torch.mm(not_protected_hessian, np_grad)
					np_sign_m_p_grads_m_hes = torch.mm(cf_not_pro_signs, np_grads_m_hes)[0]

					final_gradients = 2 * (expected_diff_protected - expected_diff_not_protected) 
					final_gradients = final_gradients * (p_sign_m_p_grads_m_hes - np_sign_m_p_grads_m_hes)

					subgroup_dif_grads.append(final_gradients)
					#########

				subgroup_dif_grads = torch.stack(subgroup_dif_grads)
				subgroup_dif_grads = torch.mean(subgroup_dif_grads,dim=0)

				# Setup gradient storage for jacobians
				P_network_gradient = []
				for param in model.parameters():
					P_network_gradient.append(torch.zeros_like(param))

				torch.nn.utils.vector_to_parameters(subgroup_dif_grads, P_network_gradient)

				done = True
		else:
			done = False

		# Final loss
		total_loss = loss 

		if WRITER:
			writer.add_scalar('Loss/bce_loss', loss, e)
			writer.add_scalar('Loss/total_loss', total_loss, e)

		if done:
			optim.zero_grad()
			loss.backward()
			for q, param in enumerate(model.parameters()):
				param.grad.data -= DIST * P_network_gradient[q]
			optim.step()

		# Cleanup
		for d in derivatives:
			del d

		with torch.no_grad():
			predictions = model(data_t)[:,0]
			arg_preds = (predictions >= 0.5).int()
			test_acc = (torch.sum(arg_preds == labels_t).float() / labels_t.shape[0])

			predictions = model(data)[:,0]
			arg_preds = (predictions >= 0.5).int()
			train_acc = (torch.sum(arg_preds == labels).float() / labels.shape[0])

			if WRITER:
				writer.add_scalar('Accuracy/train_acc', train_acc, e)
				writer.add_scalar('Accuracy/testing_acc', test_acc, e)
				print ("Test Acc", test_acc)

			tr_protected_negative, tr_nonprotected_negative = get_groups(data, model(data)[:,0], protected)
			te_protected_negative, te_nonprotected_negative = get_groups(data_t, model(data_t)[:,0], protected_t)

			training_diff = assess(e, 
								   model, 
								   data, 
								   protected, 
								   labels, 
								   data_t, 
								   protected_t, 
								   labels_t, 
								   CFNAME, 
								   config, 
								   writer, 
								   noise, 
								   WRITER,
								   df=df,
								   verbose=True,
								   r=True)

			training_diff = training_diff['Training_Delta'] 
