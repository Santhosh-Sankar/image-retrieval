# Large Scale image retrieval using attention

This repository contains files used for training Resnet-101 models independently coupled with Spatial attention module, Channel attention module, Squeeze and Excitation(SE) module  and convolution block attention(CBAM) module to 

Different ensemble of models were tries and tested and the ensemble with the combination of Spatial attention module, Squeeze and Excitation(SE) module  and convolution block attention(CBAM) module provided the highers mean average precision(mAP).

The models were developed and trained using TensorFlow utilizing high performance computing GPU clusters (NVIDIA P100 and A100). 

## Datasets

The datasets used are summarized below.

| **Purpose**    | **Dataset**                       |
| :------------: | :-------------------------------: |
| **Training**   | Google Landmark Dataset v2(clean) |
| **Evaluation** | Revisited Oxford and Paris dataset|


## Evaluation Results

The below table summarizes the mAP obtained after coupling ResNet-101 with different attention modules indivitually.

| **Attention module** | **mAP**  |
| :------------------: | :------: |
| **Spatial**          | 9.96     |
| **Channel**          | 10.72    |
| **CBAM**             | 11.36    |
| **SE**               | 11.89    |

The below table summarizes the mAP obtained after coupling ResNet-101 with different ensemble of attention modules.

| **Ensemble**                      | **mAP**  |
| :-------------------------------: | :------: |
| **SE + CBAM**                     | 13.02    |
| **SE + CBAM + Spatial + Channel** | 13.35    | 
| **SE + CBAM + Spatial**           | 16.29    |


## Retrieval Results


### Resnet-101 with Squeeze and Excitation(SE) module 

<p align='center'>
    <img src="/images/se.png" alt="animation" width="1000"/>
</p>

### Resnet-101 with Convolution block attention(CBAM) module

<p align='center'>
    <img src="/images/cbam.png" alt="animation" width="1000"/>
</p>

### Resnet-101 with ensemble of SE, CBAM, Spatial and Channel attention modules

<p align='center'>
    <img src="/images/ensemble.png" alt="animation" width="1000"/>
</p>


## References

- [Google_landmark_retrieval_Challenge] (https://www.kaggle.com/competitions/landmark-retrieval-2021)
- [Google_Landmark_Dataset_v2] (https://github.com/cvdfoundation/google-landmark) 
- [Revisited_Oxford_and_Paris_Dataset] (http://cmp.felk.cvut.cz/revisitop/)