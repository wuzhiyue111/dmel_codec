import hydra
import torch
from hydra.utils import instantiate
from omegaconf import OmegaConf

# initial speechtokenizer | DAC | Encodec | Ours_dMel_codec | mimi(moshi codec) | fish_speech
class InitialCodec:
    def __init__(
        self,
        codec_name: str,
        ckpt_path: str = None,
        device: str = "cpu",
        sample_rate: int = 24000,
        num_quantizers: int | None = None,
        config_path: str = None,
    ):
        self.codec_name = codec_name
        self.ckpt_path = ckpt_path
        self.device = device
        self.sample_rate = sample_rate
        self.num_quantizers = num_quantizers
        self.config_path = config_path
        self.hparams_check()
        if self.codec_name == "speechtokenizer":
            from speechtokenizer.model import SpeechTokenizer

            self.codec = SpeechTokenizer.load_from_checkpoint(
                ckpt_path + "/config.json",
                ckpt_path + "/SpeechTokenizer.pt",
            )
            self.sample_rate = self.codec.sample_rate

        elif self.codec_name == "DAC":
            import dac
            model_path = dac.utils.download(model_type=f"{self.sample_rate//1000}khz")
            self.codec = dac.DAC.load(model_path)

        elif self.codec_name == "dMel":
            from dmel_codec.train_codec import get_config

            config = get_config()
            self.codec = hydra.utils.instantiate(config.model, _convert_="partial")
            self.codec.load_state_dict(torch.load(self.ckpt_path)["state_dict"], strict=False) # vocoder have loaded in init

        elif self.codec_name == "mimi":
            from transformers.models.mimi.modeling_mimi import MimiModel
            from transformers.models.mimi.configuration_mimi import MimiConfig

            config = MimiConfig.from_pretrained(self.ckpt_path)
            config.use_cache = True
            self.codec = MimiModel.from_pretrained(self.ckpt_path, config=config)
        
        elif self.codec_name == "fish_speech":
            '''
            config_path: 默认是 /home/wzy/projects/zcf_projects/dmel_codec/dmel_codec/config/fish_speech_configs/firefly_gan_vq.yaml
                可以从fish speech的官方库： fish-speech-1.4.3/fish_speech/configs 中找到
            ckpt_path: 默认是 /home/wzy/projects/zcf_projects/dmel_codec/checkpoints/fish-speech-1.4/firefly-gan-vq-fsq-8x1024-21hz-generator.pth
                可以从 https://huggingface.co/fishaudio/fish-speech-1.4/tree/main 中找到，手动下载即可
            '''
            self.codec = load_fish_speech_model(config_path, ckpt_path)
            self.sample_rate = self.codec.spec_transform.sample_rate

        self.codec.to(self.device)
        self.codec.eval()

    def hparams_check(self):
        assert self.codec_name in [
            "speechtokenizer",
            "DAC",
            "dMel",
            "mimi",
            "fish_speech",
        ], "Invalid codec name, assert codec_name in ['speechtokenizer', 'DAC', 'Encodec', 'dMel', 'mimi', 'fish_speech']"
        if self.codec_name == "dMel":
            assert (
                self.ckpt_path is not None
            ), "ckpt_path must be provided for dMel codec"
        if self.codec_name == "mimi":
            assert self.ckpt_path is not None, "ckpt_path must be provided for mimi codec"
        
        if self.num_quantizers is None:
            print("No num_quantizers provided, using default quantizers")
        else:
            print(f"Using {self.num_quantizers} quantizers")
        
        if self.codec_name == "fish_speech":
            assert self.config_path is not None, "config_path must be provided for fish_speech codec"
            assert self.ckpt_path is not None, "ckpt_path must be provided for fish_speech codec"

    @torch.inference_mode()
    def extract_indices(self, audios: torch.Tensor, audio_lens: torch.Tensor):
        # audios.shape = (batch_size, 1, audio_len)
        # audio_lens.shape = (batch_size,)
        feature_lens = None
        if self.codec_name == "dMel":
            indices, feature_lens = self.codec.encode(audios, audio_lens)
        elif self.codec_name == "speechtokenizer":
            indices = self.codec.encode(audios)
            print("warning: speechtokenizer indices shape is (codebook_num, batch_size, audio_len)")

        elif self.codec_name == "DAC":
            _, indices, _, _, _ = self.codec.encode(audios)

        elif self.codec_name == "mimi":
            indices, _ = self.codec._encode_frame(input_values = audios, num_quantizers=self.num_quantizers, padding_mask=None)
        
        elif self.codec_name == "fish_speech":
            # 默认传入的音频已经被重采样到模型需要的频率了，第二个省略的参数是 feature_lens, 但是这个 feature_lens 可能存在偏差，因此手动获得更加准确
            indices, _ = self.codec.encode(audios, audio_lens) # [b, c, t]
            feature_lens = torch.tensor([indices.shape[-1]], device=self.device)

        else:
            raise NotImplementedError(f"Extract indices not implemented for {self.codec_name}")

        return indices, feature_lens

    @torch.inference_mode()
    def extract_latent_unquantized(self, audios: torch.Tensor, audio_lens: torch.Tensor):
        mel_lengths = None
        if self.codec_name == "dMel":
            unquantized_features, mel_lengths = self.codec.encode_unquantized(audios, audio_lens)

        elif self.codec_name == "speechtokenizer":
            unquantized_features = self.codec.encoder(audios)

        elif self.codec_name == "DAC":
            unquantized_features = self.codec.encoder(audios)

        elif self.codec_name == "mimi":
            embeddings = self.codec.encoder(audios)
            encoder_outputs = self.codec.encoder_transformer(
                embeddings.transpose(1, 2), past_key_values=None, return_dict=None
            )
            encoder_outputs = encoder_outputs[0].transpose(1, 2)
            unquantized_features = self.codec.downsample(encoder_outputs)
        
        elif self.codec_name == "fish_speech":
            # NOTE mel_lengths 和feature length不同
            audios = audios.float()
            mels = self.codec.spec_transform(audios)
            mel_lengths = audio_lens // self.codec.spec_transform.hop_length
            mel_masks = self.sequence_mask(mel_lengths, mels.shape[2])
            mel_masks_float_conv = mel_masks[:, None, :].float()
            mels = mels * mel_masks_float_conv
            # Encode
            unquantized_features = self.codec.backbone(mels) * mel_masks_float_conv

        else:
            raise NotImplementedError(f"Extract latent unquantized not implemented for {self.codec_name}")

        return unquantized_features, mel_lengths

    @torch.inference_mode()
    def extract_latent_quantized(self, audios: torch.Tensor, audio_lens: torch.Tensor):
        mel_masks_float_conv = None
        if self.codec_name == "dMel":
            unquantized_features, mel_lengths = self.codec.encode_unquantized(audios, audio_lens)
            indices, indices_lengths = self.codec.get_indices_from_unquantized_features(unquantized_features, mel_lengths)
            quantized_features, mel_masks_float_conv = self.codec.get_quantized_features_from_indices(indices, indices_lengths)
            
        elif self.codec_name == "speechtokenizer":
            quantized_list = self.codec.forward_feature(audios)
            quantized_features = 0.0
            for quantized in quantized_list:
                quantized_features += quantized

        elif self.codec_name == "DAC":
            quantized_features, _, _, _, _ = self.codec.encode(audios)

        elif self.codec_name == "mimi":
            indices, _ = self.codec._encode_frame(input_values = audios, num_quantizers=self.num_quantizers, padding_mask=None)
            quantized_features = self.codec.quantizer.decode(indices)
        
        elif self.codec_name == "fish_speech":
            indices, _ = self.codec.encode(audios, audio_lens) # [b, c, t]
            feature_lengths = torch.tensor([indices.shape[-1]], device=self.device)
            downsample_factor = self.codec.downsample_factor

            mel_masks = self.sequence_mask(
                feature_lengths * downsample_factor,
                indices.shape[2] * downsample_factor,
            )
            mel_masks_float_conv = mel_masks[:, None, :].float()

            # NOTE fish speech代码中需要乘以 mel_masks_float_conv
            quantized_features = self.codec.quantizer.decode(indices) * mel_masks_float_conv

        else:
            raise NotImplementedError(f"Extract latent quantized not implemented for {self.codec_name}")

        return quantized_features, mel_masks_float_conv

    @torch.inference_mode()
    def rec_audio_from_indices(self, indices: torch.Tensor, indices_lengths: torch.Tensor):
        '''
        indices: [B, C, L]
        '''
        gen_mel = None
        if self.codec_name == "dMel":
            rec_audios, gen_mel = self.codec.decode(indices, indices_lengths, return_audios=True)

        elif self.codec_name == "speechtokenizer":
            rec_audios = self.codec.decode(indices)

        elif self.codec_name == "DAC":
            quantized_features, _, _ = self.codec.quantizer.from_codes(indices)
            rec_audios = self.codec.decode(quantized_features)

        elif self.codec_name == "mimi":
            padding_mask = self.get_padding_mask_for_mimi(audio_lens)
            rec_audios = self.codec.decode(indices, padding_mask=padding_mask).audio_values
        
        elif self.codec_name == "fish_speech":
            # 此处被省略的参数为 audio_lengths
            rec_audios, _ = self.codec.decode(indices=indices, feature_lengths=indices_lengths)

        else:
            raise NotImplementedError(f"Rec from indices not implemented for {self.codec_name}")

        return rec_audios, gen_mel

    @torch.inference_mode()
    def rec_audio_from_audio(self, audios: torch.Tensor, audio_lens: torch.Tensor):
        gen_mel = None
        if self.codec_name == "dMel":
            indices, indices_lengths = self.codec.encode(audios, audio_lens)
            rec_audios, gen_mel = self.codec.decode(indices, indices_lengths, return_audios=True)

        elif self.codec_name == "speechtokenizer":
            indices = self.codec.encode(audios)
            print("warning: speechtokenizer indices shape is (codebook_num, batch_size, audio_len)")
            rec_audios = self.codec.decode(indices)

        elif self.codec_name == "DAC":
            rec_audios = self.codec(audios, n_quantizers=self.num_quantizers)['audio']

        elif self.codec_name == "mimi":
            padding_mask = self.get_padding_mask_for_mimi(audio_lens)
            rec_audios = self.codec(audios, padding_mask=padding_mask).audio_values
        
        elif self.codec_name == "fish_speech":
            # 默认传入的音频已经被重采样到模型需要的频率了，第二个省略的参数是 feature_lens
            indices, _ = self.codec.encode(audios, audio_lens)
            feature_lengths = torch.tensor([indices.shape[-1]], device=self.device)
            # 此处被省略的参数为 audio_lengths
            rec_audios, _ = self.codec.decode(indices=indices, feature_lengths=feature_lengths)
        else:
            raise NotImplementedError(f"Rec from audio not implemented for {self.codec_name}")

        return rec_audios, gen_mel

    def split_from_length(self, audios: torch.Tensor, length: int):
        # There are some audios that are shorter than the audio_len, we need to split them into the corresponding length
        # audios.shape = (batch_size, 1, audio_len)
        # length.shape = (batch_size,)
        assert audios.shape[0] > 1 and audios.shape[0] == length.shape[0], "batch_size must be greater than 1 and equal to length.shape[0]"
        split_audios = []
        for i in range(audios.shape[0]):
            split_audios.append(audios[i, :, :length[i]])
        return split_audios
    
    def pad_audio_tensor_from_list(self, audios: list, audio_lens: list):
        """Pad audios and stack them into a batch tensor
        Args:
            audios: audio tensor list [(1, 1, L1), (1, 1, L2), ...]
            audio_lens: audio length list [L1, L2, ...]
        Returns:
            padded_audios: padded batch tensor (B, 1, max_len)
        """
        assert len(audios) == len(audio_lens), "audios and audio_lens must have the same length"
        max_len = max(audio_lens)
        batch_size = len(audios)

        device = self.device
        padded_audios = torch.zeros(batch_size, 1, max_len, device=device)
        
        # fill values
        for i, (audio, length) in enumerate(zip(audios, audio_lens)):
            padded_audios[i, :, :length] = audio[..., :length]
            
        return padded_audios.to(self.device), torch.tensor(audio_lens, device=self.device)
    
    def get_padding_mask_for_mimi(self, audio_lens: torch.Tensor):
        # audio_lens.shape = (batch_size,)
        batch_size, max_len = audio_lens.shape[0], audio_lens.max().item()
        mask = torch.ones((batch_size, max_len), device=self.device)

        # fill values
        for i in range(batch_size):
            mask[i, audio_lens[i]:] = 0

        # expand dims to match input requirements (batch_size, 1, max_len)
        mask = mask.unsqueeze(1)

        return mask.bool().to(self.device)
    
    def sequence_mask(self, length, max_length=None):
        if max_length is None:
            max_length = length.max()
        x = torch.arange(max_length, dtype=length.dtype, device=length.device)
        return x.unsqueeze(0) < length.unsqueeze(1)

def load_fish_speech_model(config_path, checkpoint_path):
    # hydra.core.global_hydra.GlobalHydra.instance().clear()
    cfg = OmegaConf.load(config_path)

    model = instantiate(cfg)
    state_dict = torch.load(
        checkpoint_path, map_location=device, mmap=True, weights_only=True
    )
    if "state_dict" in state_dict:
        state_dict = state_dict["state_dict"]

    if any("generator" in k for k in state_dict):
        state_dict = {
            k.replace("generator.", ""): v
            for k, v in state_dict.items()
            if "generator." in k
        }

    result = model.load_state_dict(state_dict, strict=False, assign=True)
    model.eval()

    # logger.info(f"Loaded model: {result}")
    return model



if __name__ == "__main__":
    import torchaudio
    from dmel_codec.utils.utils import open_filelist
    # load filelist to find audio path
    audio_path_list = open_filelist("/sdb/data1_filelist/filelist_VCTK-Corpus.txt", file_num=2) # Just test 2 audios
    
    for device in ["cpu", "cuda:0"]:
        # dmel_codec test
        dmel_codec = InitialCodec(
            codec_name="dMel",
            ckpt_path="/home/wzy/projects/dmel_codec/dmel_codec/ckpt/epoch=018-step=746000_20hz.ckpt",
            device=device,
            sample_rate=24000
        )

        audio_list = []
        audio_lens_list = []
        for audio_path in audio_path_list:
            audio, sr = torchaudio.load(audio_path)
            if sr != dmel_codec.sample_rate:
                audio = torchaudio.functional.resample(audio, sr, dmel_codec.sample_rate)
            audio = audio.unsqueeze(0).to(dmel_codec.device)
            audio_lens = torch.tensor([audio.shape[-1]]).to(dmel_codec.device)
            audio_list.append(audio)
            audio_lens_list.append(audio_lens)

        padded_audios, audio_lens = dmel_codec.pad_audio_tensor_from_list(audio_list, audio_lens_list)
        # test batch rec from audio
        _, _ = dmel_codec.rec_audio_from_audio(padded_audios, audio_lens)
        
        # test batch rec from indices
        indices, indices_lengths = dmel_codec.extract_indices(padded_audios, audio_lens)
        _, _ = dmel_codec.rec_audio_from_indices(indices, indices_lengths)

        # test batch extract latent unquantized
        _, _ = dmel_codec.extract_latent_unquantized(padded_audios, audio_lens)

        # test batch extract latent quantized
        _, _ = dmel_codec.extract_latent_quantized(padded_audios, audio_lens)

        print(f"{device} dmel_codec pass")
        del dmel_codec
        
        # speechtokenizer test
        speechtokenizer = InitialCodec(
            codec_name="speechtokenizer",
            device=device,
            ckpt_path="/sdb/model_weight/speechTokenizer/speechtokenizer_hubert_avg"
        )

        audio_list = []
        audio_lens_list = []
        for audio_path in audio_path_list:
            audio, sr = torchaudio.load(audio_path)
            if sr != speechtokenizer.sample_rate:
                audio = torchaudio.functional.resample(audio, sr, speechtokenizer.sample_rate)
            audio = audio.unsqueeze(0).to(speechtokenizer.device)
            audio_lens = torch.tensor([audio.shape[-1]]).to(speechtokenizer.device)
            audio_list.append(audio)
            audio_lens_list.append(audio_lens)

        padded_audios, audio_lens = speechtokenizer.pad_audio_tensor_from_list(audio_list, audio_lens_list)
        # test batch rec from audio
        _, _ = speechtokenizer.rec_audio_from_audio(padded_audios, audio_lens)

        # test batch rec from indices
        indices, indices_lengths = speechtokenizer.extract_indices(padded_audios, audio_lens)
        _, _ = speechtokenizer.rec_audio_from_indices(indices, indices_lengths)

        # test batch extract latent unquantized
        _, _ = speechtokenizer.extract_latent_unquantized(padded_audios, audio_lens)

        # test batch extract latent quantized
        _, _ = speechtokenizer.extract_latent_quantized(padded_audios, audio_lens)

        print(f"{device} speechtokenizer pass")
        del speechtokenizer
        
        # DAC test
        dac = InitialCodec(
            codec_name="DAC",
            device=device,
        )

        audio_list = []
        audio_lens_list = []
        for audio_path in audio_path_list:
            audio, sr = torchaudio.load(audio_path)
            if sr != dac.sample_rate:
                audio = torchaudio.functional.resample(audio, sr, dac.sample_rate)
            audio = audio.unsqueeze(0).to(dac.device)
            audio_lens = torch.tensor([audio.shape[-1]]).to(dac.device)
            audio_list.append(audio)
            audio_lens_list.append(audio_lens)

        padded_audios, audio_lens = dac.pad_audio_tensor_from_list(audio_list, audio_lens_list)
        # test batch rec from audio
        _, _ = dac.rec_audio_from_audio(padded_audios, audio_lens)
            
        # test batch rec from indices
        indices, indices_lengths = dac.extract_indices(padded_audios, audio_lens)
        _, _ = dac.rec_audio_from_indices(indices, indices_lengths)
            
        # test batch extract latent unquantized
        _, _ = dac.extract_latent_unquantized(padded_audios, audio_lens)

        # test batch extract latent quantized
        _, _ = dac.extract_latent_quantized(padded_audios, audio_lens)

        print(f"{device} DAC pass")
        del dac
        
        # mimi test
        mimi = InitialCodec(
            codec_name="mimi",
            ckpt_path="/sdb/model_weight/moshi_mimi_huggingface",
            device=device,
            sample_rate=24000
        )

        audio_list = []
        audio_lens_list = []
        for audio_path in audio_path_list:
            audio, sr = torchaudio.load(audio_path)
            if sr != mimi.sample_rate:
                audio = torchaudio.functional.resample(audio, sr, mimi.sample_rate)
            audio = audio.unsqueeze(0).to(mimi.device)
            audio_lens = torch.tensor([audio.shape[-1]]).to(mimi.device)
            audio_list.append(audio)
            audio_lens_list.append(audio_lens)

        padded_audios, audio_lens = mimi.pad_audio_tensor_from_list(audio_list, audio_lens_list)
        # test batch rec from audio
        _, _ = mimi.rec_audio_from_audio(padded_audios, audio_lens)

        # test batch rec from indices
        indices, indices_lengths = mimi.extract_indices(padded_audios, audio_lens)
        _, _ = mimi.rec_audio_from_indices(indices, indices_lengths)

        # test batch extract latent unquantized
        _, _ = mimi.extract_latent_unquantized(padded_audios, audio_lens)

        # test batch extract latent quantized
        _, _ = mimi.extract_latent_quantized(padded_audios, audio_lens)

        print(f"{device} mimi pass")
        del mimi



        # fish_speech test
        # NOTE 虽然此处传入的 sample_rate=24000， 但是在类的 init 过程中，会根据load的模型，自动调整到正确的sample_rate，也就是 44100
        fish_speech_codec = InitialCodec(
            codec_name="fish_speech",
            ckpt_path="/home/wzy/projects/zcf_projects/dmel_codec/checkpoints/fish-speech-1.4/firefly-gan-vq-fsq-8x1024-21hz-generator.pth",
            device=device,
            sample_rate=24000,
            config_path = "/home/wzy/projects/zcf_projects/dmel_codec/dmel_codec/config/fish_speech_configs/firefly_gan_vq.yaml"
        )

        audio_list = []
        audio_lens_list = []
        for audio_path in audio_path_list:
            audio, sr = torchaudio.load(audio_path)
            if sr != fish_speech_codec.sample_rate:
                audio = torchaudio.functional.resample(audio, sr, fish_speech_codec.sample_rate)
            audio = audio.unsqueeze(0).to(fish_speech_codec.device)
            audio_lens = torch.tensor([audio.shape[-1]], dtype=torch.long).to(fish_speech_codec.device)
            audio_list.append(audio)
            audio_lens_list.append(audio_lens)

        padded_audios, audio_lens = fish_speech_codec.pad_audio_tensor_from_list(audio_list, audio_lens_list)
        # test batch rec from audio
        _, _ = fish_speech_codec.rec_audio_from_audio(padded_audios, audio_lens)

        # test batch rec from indices
        indices, indices_lengths = fish_speech_codec.extract_indices(padded_audios, audio_lens)
        _, _ = fish_speech_codec.rec_audio_from_indices(indices, indices_lengths)

        # test batch extract latent unquantized
        _, _ = fish_speech_codec.extract_latent_unquantized(padded_audios, audio_lens)

        # test batch extract latent quantized
        _, _ = fish_speech_codec.extract_latent_quantized(padded_audios, audio_lens)

        print(f"{device} fish_speech pass")
        del fish_speech_codec
