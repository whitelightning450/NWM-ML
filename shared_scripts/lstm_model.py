# hydrological packages
import hydroeval as he
from hydrotools.nwm_client import utils 
from tqdm.notebook import tqdm_notebook

# basic packages
import numpy as np
import pandas as pd
import os
import pyarrow as pa
import pyarrow.parquet as pq
import bz2file as bz2

# system packages
from progressbar import ProgressBar
from datetime import datetime, date
import datetime
import pickle as pkl
import warnings
warnings.filterwarnings("ignore")
import platform
import time

# data analysi packages
from scipy import optimize
from sklearn.metrics import mean_absolute_percentage_error
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error
from sklearn.metrics import mean_absolute_percentage_error
from sklearn.model_selection import GridSearchCV
from sklearn.model_selection import RepeatedKFold
from sklearn.model_selection import cross_val_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
import joblib

# deep learning packages
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset
from torch.autograd import Variable


#Shared/Utility scripts
import sys
import boto3
import s3fs
sys.path.insert(0, '../..')  #sys allows for the .ipynb file to connect to the shared folder files
from shared_scripts import lstm_dataprocessing, Simple_Eval

#load access key
HOME = os.path.expanduser('~')
KEYPATH = "NWM_ML/AWSaccessKeys.csv"
ACCESS = pd.read_csv(f"{HOME}/{KEYPATH}")

#start session
SESSION = boto3.Session(
    aws_access_key_id=ACCESS['Access key ID'][0],
    aws_secret_access_key=ACCESS['Secret access key'][0],
)
S3 = SESSION.resource('s3')
#AWS BUCKET information
BUCKET_NAME = 'streamflow-app-data'
BUCKET = S3.Bucket(BUCKET_NAME)

#s3fs
fs = s3fs.S3FileSystem(anon=False, key=ACCESS['Access key ID'][0], secret=ACCESS['Secret access key'][0])

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print("Device:", DEVICE)

def lstm_model_arch(bidirectional, input_shape, neurons, num_layers):
    # Build the model
    model = nn.LSTM(input_size=input_shape, 
                    hidden_size=neurons, 
                    num_layers = num_layers,
                    bidirectional=bidirectional, 
                    batch_first=True).to(DEVICE)
    if bidirectional == True: # Multiply by 2 for bidirectional LSTM
        neurons = neurons * 2
    fc = nn.Linear(neurons, 1).to(DEVICE)  


    return model, fc


def LSTM_train(model_params, loss_func, x_train_scaled_t, y_train_scaled_t, shuffle, model_path,modelname):
    epochs, batch_size, learning_rate, decay, neurons, num_layers, bidirectional = model_params
    print(f"Epochs: {epochs}, Batch size: {batch_size}, LR: {learning_rate}, Decay: {decay}, Neurons: {neurons}, Number Layers: {num_layers}, Bidirectional: {bidirectional}")

    input_shape = x_train_scaled_t.shape[2]
    start_time = time.time()

    # Create PyTorch datasets and dataloaders
    torch.manual_seed(69)
    train_dataset = TensorDataset(x_train_scaled_t, y_train_scaled_t)
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=shuffle ) #

    # Build the model, 
    model, fc = lstm_model_arch(bidirectional, input_shape, neurons, num_layers)

    # Define loss and optimizer - change loss criterian (e.g. MSE), differnt optizers
    criterion = loss_func
    optimizer = optim.Adam(model.parameters(), lr=learning_rate, weight_decay=decay) #

    # Training loop 
    for epoch in tqdm_notebook(range(epochs), desc= "Epochs completed"):
        model.train()
        fc.train()
        total_loss = 0.0
        for batch_x, batch_y in train_loader:
            batch_x, batch_y = batch_x.to(DEVICE), batch_y.to(DEVICE)
            output, _ = model(batch_x)
            output = fc(output[:, -1, :])
            loss = criterion(output, batch_y)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        print(f"Epoch {epoch + 1}/{epochs}, Loss: {total_loss / len(train_loader)}") # # out once model begins showing loss

    print('finish')
    print("Run Time:" + " %s seconds " % ((time.time() - start_time)))
    if os.path.exists(model_path) == False:
        os.mkdir(model_path)
    torch.save(model.state_dict(), f"{model_path}/{modelname}_model.pkl")
    torch.save(fc.state_dict(), f"{model_path}/{modelname}_model_fc.pkl")


def LSTM_predict(model_params, test_years, df, X_test_dic, input_shape, StreamStats, model_path, modelname):
    bidirectional, input_shape, neurons, num_layers = model_params

    scalername_y = "scaler_y.save"
    y_scaler_path = f"{model_path}/{scalername_y}"
    #get dataframe for testing
    x_test_temp = df[df.datetime.dt.year.isin(test_years)]


    model, fc = lstm_model_arch(bidirectional, input_shape, neurons, num_layers)

    #this requires the model structure to be preloaded
    model.load_state_dict(torch.load(f"{model_path}/{modelname}_model.pkl"))
    fc.load_state_dict(torch.load(f"{model_path}/{modelname}_model_fc.pkl"))

    #load y scaler
    scaler_y = joblib.load(y_scaler_path)

    #get prediction locations
    station_index_list = list(X_test_dic.keys())


    Preds_Dict = {}
    for station_number in station_index_list:
        index = station_index_list = station_number
        X_test_scaled_t = Variable(torch.from_numpy(X_test_dic[index]).float(), requires_grad=False).to(DEVICE)

        # Evaluation
        model.eval()
        with torch.no_grad():
            predictions_scaled, _ = model(X_test_scaled_t)
            predictions_scaled = fc(predictions_scaled[:, -1, :])

        # Invert scaling for actual
        predictions = scaler_y.inverse_transform(predictions_scaled.to('cpu').numpy())
        predictions[predictions<0] = 0
        predictions = pd.DataFrame(predictions, columns=[f"{modelname}_flow"])

        #save predictions, need to convert to NHDPlus reach - Need to add Datetime column and flow predictions
        #make daterange
        dates = pd.date_range(pd.to_datetime(f"{test_years[0]}-01-01"), periods=len(predictions)).strftime("%Y-%m-%d").tolist()
        predictions['Datetime'] = dates
            
        #get reach id for model eval
        nhdreach = utils.crosswalk(usgs_site_codes=station_number)
        nhdreach = nhdreach['nwm_feature_id'].iloc[0]

        #put columns in correct order
        cols = ['Datetime', f"{modelname}_flow"]
        predictions = predictions[cols]

        #save predictions to AWS so we can use CSES
        state = StreamStats['state_id'][StreamStats['NWIS_site_id'].astype(str)== station_number].values[0].lower()
        csv_key = f"{modelname}/NHD_segments_{state}.h5/{modelname[:3]}_{nhdreach}.csv"
        predictions.to_csv(f"s3://{BUCKET_NAME}/{csv_key}", index = False,  storage_options={'key': ACCESS['Access key ID'][0],
                                'secret': ACCESS['Secret access key'][0]})

        #Concat DFS and put into dictionary
        x_test_temp['nwm_feature_id'] = nhdreach
        Dfs = [predictions.reset_index(drop=True),x_test_temp[x_test_temp['station_id']==station_number].reset_index(drop=True)]
        Preds_Dict[station_number] = pd.concat(Dfs, axis=1)

        #reorganize columns
        Preds_Dict[station_number].pop('datetime')
        Preds_Dict[station_number].insert(1, f"{modelname}_flow", Preds_Dict[station_number].pop(f"{modelname}_flow"))
        Preds_Dict[station_number].insert(1, "NWM_flow", Preds_Dict[station_number].pop("NWM_flow"))
        Preds_Dict[station_number].insert(1, "flow_cfs", Preds_Dict[station_number].pop("flow_cfs"))
        Preds_Dict[station_number].insert(1, "nwm_feature_id", Preds_Dict[station_number].pop("nwm_feature_id"))
        Preds_Dict[station_number].insert(1, "station_id", Preds_Dict[station_number].pop("station_id")) 
        Preds_Dict[station_number].set_index('Datetime', inplace = True)


    #save predictions as compressed pkl file
    pred_path = f"{HOME}/NWM_ML/Predictions/Hindcast/{modelname}/Multilocation"
    file_path = f"{pred_path}/{modelname}_predictions.pkl"
    if os.path.exists(pred_path) == False:
        os.makedirs(pred_path)

    with open(file_path, 'wb') as handle:
        pkl.dump(Preds_Dict, handle, protocol=pkl.HIGHEST_PROTOCOL)

    return Preds_Dict


def LSTM_optimization(df, 
                    input_columns, 
                    target, 
                    test_years, 
                    model_path,
                    scalertype,
                    training_params,
                    loss_func,
                    shuffle,  
                    modelname,
                    StreamStats,
                    supply):
    
    epochs, lookback, batch_size, learning_rate, decay, neurons,num_layers, bidirectional = training_params
    
    GS_Eval_DF = pd.DataFrame()
    GS_Eval_dict = {}
    GS_Pred_dict = {}

    n_models = len(epochs)*len(batch_size)*len(learning_rate)*len(decay)*len(neurons)*len(bidirectional)*len(lookback)
    print(f"Optimizing the {modelname} model by evaluating {n_models} models using grid search validation")

    counter = 1

    #loop through the differnet model parameters
    for e in epochs:
        for look in lookback:
               #add scaler option - minmax standard
            x_train_scaled_t, X_test_dic, y_train_scaled_t, y_test_dic = lstm_dataprocessing.Multisite_DataProcessing(df, 
                                                                                   input_columns, 
                                                                                   target, 
                                                                                   look, 
                                                                                   test_years, 
                                                                                   model_path,
                                                                                   scalertype) 
            input_shape = x_train_scaled_t.shape[2]
            for b in batch_size:
                for lr in learning_rate:
                    for d in decay:
                        for n in neurons:
                            for l in num_layers:
                                for bi in bidirectional:
                                    #Train the model
                                    print(f"Training {counter} of {n_models} models")
                                    params = e, b, lr, d, n, l, bi
                                    model_params = bi, input_shape, n, l
                                    print(f"Lookback: {lookback}")
                                    print(f"feature shape: {x_train_scaled_t.shape}, Test shape: {y_train_scaled_t.shape}")
                                    
                                    LSTM_train(params,
                                            loss_func,
                                                x_train_scaled_t,
                                                y_train_scaled_t, 
                                                shuffle, 
                                                model_path,
                                                modelname)

                                    Preds_Dict = LSTM_predict(model_params, 
                                                            test_years, 
                                                            df, 
                                                            X_test_dic, 
                                                            input_shape, 
                                                            StreamStats, 
                                                            model_path, 
                                                            modelname)

                                    #Evaluate model performance of the different models, 'flow_cfs_pred', 
                                    prediction_columns = ['NWM_flow', f"{modelname}_flow"]
                                    Eval_DF = Simple_Eval.Simple_Eval(Preds_Dict, 
                                                                    prediction_columns, 
                                                                    modelname, 
                                                                    supply = supply,
                                                                    plots = False, 
                                                                    keystats = False        
                                                                    )

                                    #create dataframe to store key model perf metrics, and inputs
                                    cols = [f"{modelname}_flow_kge", f"{modelname}_flow_rmse", f"{modelname}_flow_mape", f"{modelname}_flow_pbias"]
                                    model_eval = Eval_DF[cols].copy()

                                    #Get mean scoring metrics for AOI - aver kge, mape, pbias
                                    model_eval = pd.DataFrame(model_eval.mean(axis=0)).T

                                    #Add model parameters
                                    parm_dict = {'Epochs': [e],
                                                 "Lookback":[look],
                                                'Batchsize': [b],
                                                'LR': [lr],
                                                'Decay':[d],
                                                'Neurons':[n],
                                                'Bidirectional':[bi],
                                                'num_layers':[l]}
                                    params_df = pd.DataFrame.from_dict(parm_dict)

                                    #combine model eval df with params df
                                    model_df = pd.concat([model_eval, params_df], axis = 1)
                                    kge = round(model_df[f"{modelname}_flow_kge"].values[0],2)

                                    display(Eval_DF)

                                    #add to overall df
                                    GS_Eval_DF = pd.concat([GS_Eval_DF, model_df])
                                    GS_Eval_dict[kge] = Eval_DF
                                    GS_Pred_dict[kge] = Preds_Dict
                                    counter = counter +1
    #Sort by kge
    GS_Eval_DF.sort_values(by = f"{modelname}_flow_kge", ascending = False, inplace = True)
    GS_Eval_DF.reset_index(inplace=True, drop = True)

    return GS_Eval_DF, GS_Eval_dict, GS_Pred_dict


def Final_Model(df,
                GS_Eval_DF,
                x_train_scaled_t,
                y_train_scaled_t, 
                loss_func, 
                model_path, 
                modelname,
                test_years, 
                X_test_dic,
                StreamStats,
                supply,
                shuffle):
    
    #set optimial model params    
    epochs = GS_Eval_DF['Epochs'].values[0] # 
    batch_size = int(GS_Eval_DF['Batchsize'].values[0])
    learning_rate = GS_Eval_DF['LR'].values[0]
    decay = GS_Eval_DF['Decay'].values[0]   
    num_layers = GS_Eval_DF['num_layers'].values[0]
    bidirectional = GS_Eval_DF['Bidirectional'].values[0]  
    neurons = int(GS_Eval_DF['Neurons'].values[0])
    input_shape = x_train_scaled_t.shape[2]

    if bidirectional == 'True':
        bidirectional = True
    else:
        bidirectional = False

    params = epochs, batch_size, learning_rate, decay, neurons, num_layers, bidirectional
    model_params = bidirectional, input_shape, neurons, num_layers

    #Train the model with optimized parameters
    LSTM_train(params,
            loss_func,
            x_train_scaled_t,
            y_train_scaled_t, 
            shuffle, 
            model_path,
            modelname)

    Preds_Dict = LSTM_predict(model_params, 
                            test_years, 
                            df, 
                            X_test_dic, 
                            input_shape, 
                            StreamStats, 
                            model_path, 
                            modelname)

    #Evaluate model performance of the different models, 'flow_cfs_pred', 
    prediction_columns = ['NWM_flow', f"{modelname}_flow"]
    Eval_DF = Simple_Eval.Simple_Eval(Preds_Dict, 
                                    prediction_columns, 
                                    modelname, 
                                    supply = supply,
                                    plots = False, 
                                    keystats = False        
                                    )

    return Eval_DF, Preds_Dict