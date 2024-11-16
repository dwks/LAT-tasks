from transformers import AutoTokenizer, AutoModelForCausalLM
import datasets
import torch
import json
import pandas as pd
import openai
from dotenv import load_dotenv
import os
from datetime import datetime
from tasks.task import Task
from tasks.inference_utils import custom_generate, get_final_logits
from tqdm import tqdm
from collections import defaultdict
from torch.utils.data import DataLoader
from tasks.general_capabilities.templates import *

GENERAL_SYSTEM_PROMPT = """You are a helpful, respectful and honest assistant. Always answer as helpfully as possible, while being safe. Your answers should not include any harmful, unethical, racist, sexist, toxic, dangerous, or illegal content. Please ensure that your responses are socially unbiased and positive in nature.

If a question does not make any sense, or is not factually coherent, explain why instead of answering something not correct. If you don't know the answer to a question, please don't share false information."""

def number_to_letter(number):
    return chr(number + 65)


class MultipleChoiceQuestion(Task):
    """
    A class to run evaluations on any Multiple Choice QA task, such as MMLU.

    Datasets should have "prompt" and "label" columns. label should be an integer from 0 to (num-choices). prompt should be formatted in init function.
    """

    def __init__(
        self,
        batch_size,
        tokenizer,
        device='cuda',
        
        use_answer_choice_space=True
    ):
        self.batch_size = batch_size
        self.tokenizer = tokenizer
        self.device = device
        self.use_answer_choice_space = use_answer_choice_space
        
    
    def get_answer_choices(self, tokenizer, num_choices=4):
        # by default, have it be A, B, C, D
        # answer_tokens = tokenizer([" A", " B", " C", " D"], padding=True, truncation=True)
        # adapt for any number of choices
        if self.use_answer_choice_space:
            choices = [f" {chr(65 + i)}" for i in range(num_choices)]
        else:
            choices = [f"{chr(65 + i)}" for i in range(num_choices)]
        answer_tokens = tokenizer(choices, padding=True, truncation=True, return_tensors="pt").input_ids
        # print(f"{answer_tokens=}")
        return answer_tokens[:, -1]

    
    def get_test_accuracy(self, model, use_test_data=True, check_all_logits=True, continuous=False):
        if hasattr(self, 'evaluation_kwargs') and self.evaluation_kwargs is not None and isinstance(self.evaluation_kwargs, dict):
            use_test_data = self.evaluation_kwargs.get("use_test_data", use_test_data)
            check_all_logits = self.evaluation_kwargs.get("check_all_logits", check_all_logits)
            continuous = self.evaluation_kwargs.get("continuous", continuous)
        
        batch = self.get_batch(train=not use_test_data)
        prompts, labels = batch["prompt"], batch["label"]
        
        with torch.no_grad():
            last_logits = get_final_logits(model, self.tokenizer, prompts)
        
        possible_choices = self.get_answer_choices(self.tokenizer).to(self.device)
        # print(f"Decoded last logits: {self.tokenizer.batch_decode(last_logits.argmax(dim=-1))}")
        # print(f"Last logit tokens: {last_logits.argmax(dim=-1)}")
        # print(f"Labels: {labels}, possible choices: {possible_choices}")
        # print()

        if check_all_logits:
            tokenized_labels = possible_choices[labels]
            if not continuous:
                num_correct = (torch.argmax(last_logits, dim=1) == tokenized_labels).sum().item()
                return num_correct / len(labels)
            else:
                probabilities = torch.softmax(last_logits, dim=1)
                return probabilities[range(len(labels)), tokenized_labels].mean().item()
            
        else:
            answer_logits = last_logits[:, possible_choices] # shape (batch_size, num_choices)
            if not continuous:
                num_correct = (torch.argmax(answer_logits, dim=1) == labels.to(self.device)).sum().item()
                return num_correct / len(labels)
            else:
                probabilities = torch.softmax(answer_logits, dim=1)
                probabilities /= probabilities.sum(dim=1, keepdim=True)
                return probabilities[range(len(labels)), labels].mean().item()


MMLU_INPUT_FORMAT = """

"""
class MMLUTask(MultipleChoiceQuestion):

    def __init__(
        self,
        batch_size,
        tokenizer,
        device='cuda',
        question_format=None,
        subject="all",
        streaming=False,
        tiny=True,
        shuffle=False
    ):
        """
        Defaults to tiny MMLU dataset https://huggingface.co/tinyBenchmarks
        """
        super().__init__(batch_size=batch_size, tokenizer=tokenizer, device=device, use_answer_choice_space=True)

        dataset_name = "tasksource/mmlu" if not tiny else "tinyBenchmarks/tinyMMLU"

        if not streaming and not tiny:
            raise ValueError("Loading the full MMLU dataset, for speed use streaming=True or tiny=True.")

        available_subjects = [
            "abstract_algebra",
            "anatomy",
            "astronomy",
            "business_ethics",
            "clinical_knowledge",
            "college_biology",
            "college_chemistry",
            "college_computer_science",
            "college_mathematics",
            "college_medicine",
            "college_physics",
            "computer_security",
            "conceptual_physics",
            "econometrics",
            "electrical_engineering",
            "elementary_mathematics",
            "formal_logic",
            "global_facts",
            "high_school_biology",
            "high_school_chemistry",
            "high_school_computer_science",
            "high_school_european_history",
            "high_school_geography",
            "high_school_government_and_politics",
            "high_school_macroeconomics",
            "high_school_mathematics",
            "high_school_microeconomics",
            "high_school_physics",
            "high_school_psychology",
            "high_school_statistics",
            "high_school_us_history",
            "high_school_world_history",
            "human_aging",
            "human_sexuality",
            "international_law",
            "jurisprudence",
            "logical_fallacies",
            "machine_learning",
            "management",
            "marketing",
            "medical_genetics",
            "miscellaneous",
            "moral_disputes",
            "moral_scenarios",
            "nutrition",
            "philosophy",
            "prehistory",
            "professional_accounting",
            "professional_law",
            "professional_medicine",
            "professional_psychology",
            "public_relations",
            "security_studies",
            "sociology",
            "us_foreign_policy",
            "virology",
            "world_religions",
        ]

        if subject == "all" and not tiny:
            dataset_list = []
            print("Loading MMLU dataset...")
            for subject in tqdm(available_subjects):
                dataset = datasets.load_dataset(
                    "tasksource/mmlu",
                    subject,
                    split="test",
                    streaming=streaming
                )
                dataset_list.append(dataset)
            print("Concatenating datasets...")
            self.dataset = datasets.concatenate_datasets(dataset_list)
        else:
            self.dataset = datasets.load_dataset(
                dataset_name,
                subject,
                split="test",
                streaming=streaming
            )
        # rename input_formatted to prompt, answer to label
        self.dataset = self.dataset.rename_column("input_formatted", "prompt")
        self.dataset = self.dataset.rename_column("answer", "label")

        self.set_loaders(train_data=self.dataset, test_data=self.dataset, shuffle=shuffle)
        
def run_general_evals(model, model_type="llama2", evals_to_include=["MMLU"], verbose=False, batch_size=10, device="cuda"):
    if model_type == "zephyr":
        tokenizer = AutoTokenizer.from_pretrained("HuggingFaceH4/zephyr-7b-beta")
        tokenizer.pad_token_id = tokenizer.eos_token_id
    elif model_type == "llama2":
        tokenizer = AutoTokenizer.from_pretrained("meta-llama/Llama-2-7b-chat-hf")
        tokenizer.pad_token_id = tokenizer.unk_token_id
    elif model_type == "llama3":
        tokenizer = AutoTokenizer.from_pretrained("meta-llama/Meta-Llama-3-8B-Instruct")
        tokenizer.pad_token_id = tokenizer.eos_token_id
    elif model_type == "pythia":
        tokenizer = AutoTokenizer.from_pretrained("EleutherAI/pythia-2.8B")
        tokenizer.pad_token_id = tokenizer.eos_token_id
    elif model_type == "gemma":
        tokenizer = AutoTokenizer.from_pretrained("google/gemma-7b")
        tokenizer.pad_token_id = tokenizer.eos_token_id    
    elif model_type == "gemma-2":
        tokenizer = AutoTokenizer.from_pretrained("google/gemma-2-9b")
        tokenizer.pad_token_id = tokenizer.eos_token_id
    tokenizer.padding_side = "right"

    accuracy_dict = {}

    for eval_name in evals_to_include:
        if eval_name == "MMLU":
            mmlu = MMLUTask(batch_size=batch_size, tokenizer=tokenizer, device=device)
            accuracy = 0.
            n_iters = 100 // batch_size
            for i in range(n_iters):
                accuracy += mmlu.get_test_accuracy(model, use_test_data=True, check_all_logits=False, continuous=False)
            accuracy_dict["MMLU"] = accuracy / n_iters
        else:
            raise NotImplementedError(f"Evaluation {eval_name} not implemented.")    
        if verbose:
            print(f"{eval_name} accuracy is {accuracy_dict[eval_name]}")
    
    return accuracy_dict

'''
class SciQTask(MultipleChoiceQuestion):

    def __init__(
        self,
        batch_size,
        tokenizer,
        device='cuda',
        streaming=True,
        shuffle=False,
        question_format=None,
    ):
        super().__init__(batch_size=batch_size, tokenizer=tokenizer, device=device, use_answer_choice_space=True)

        if self.question_format is None:
            self.question_format = DEFAULT_4_QUESTION_FORMAT
            
        def sciq_map_fn(examples):
            questions = []
            answers = []
            for i in range(len(examples["question"])):
 
                choices = (
                    examples["correct_answer"][i],
                    examples["distractor1"][i], 
                    examples["distractor2"][i], 
                    examples["distractor3"][i]
                )

                question = self.question_format.format(
                    question=examples["question"][i],
                    choice_A=choices[i%4],
                    choice_B=choices[(i+1)%4],
                    choice_C=choices[(i+2)%4],
                    choice_D=choices[(i+3)%4],
                )
                questions.append(question)
                answers.append(number_to_letter(3-(i-1)%4))
            return {
                "question": questions,
                "answer": answers
            }
        
        self.dataset = datasets.load_dataset(
            "allenai/sciq",
            streaming=streaming,
            split="test",
        )
        self.dataset = self.dataset.map(
            sciq_map_fn,
            batched=True,
            remove_columns=set(self.dataset.column_names) - {"question", "answer"}
        )
        self.dataset = self.dataset.shuffle(seed=42)


class HellaSwagTask(MultipleChoiceQuestion):

    def __init__(
        self,
        question_format=None,
        streaming=False,
        tiny=True,
    ):
        """
        Defaults to tiny HellaSwag dataset https://huggingface.co/tinyBenchmarks
        """

        super().__init__(question_format=question_format)

        dataset_name = "Rowan/hellaswag" if not tiny else "tinyBenchmarks/tinyHellaswag"

        if not streaming and not tiny:
            raise ValueError("Loading the full HellaSwag dataset, for speed use streaming=True or tiny=True.")

        if self.question_format is None:
            self.question_format = DEFAULT_HELLASWAG_QUESTION_FORMAT

        def hellaswag_map_fn(examples):
            questions = []
            answers = []
            for i in range(len(examples["ctx"])):
                    
                question = self.question_format.format(
                    question=examples["ctx"][i],
                    choice_A=examples["endings"][i][0],
                    choice_B=examples["endings"][i][1],
                    choice_C=examples["endings"][i][2],
                    choice_D=examples["endings"][i][3],
                )
                questions.append(question)
                answers.append(number_to_letter(int(examples["label"][i])))

            return {
                "question": questions,
                "answer": answers
            }

        self.dataset = datasets.load_dataset(
            dataset_name,
            split="validation",
            streaming=streaming
        )
        self.dataset = self.dataset.map(
            hellaswag_map_fn,
            batched=True,
            remove_columns=set(self.dataset.column_names) - {"question", "answer"}
        )
        self.dataset = self.dataset.shuffle(seed=42)

    def get_test_accuracy(self, model, use_test_data=True, check_all_logits=True, continuous=False):
        return super().get_test_accuracy(model, use_test_data, check_all_logits, continuous)



class WinograndeTask(MultipleChoiceQuestion):


    def __init__(
        self,
        question_format=None,
        streaming=False,
        tiny=True,
    ):
        """
        Defaults to tiny Winogrande dataset https://huggingface.co/tinyBenchmarks
        """
        super().__init__(question_format=question_format)

        dataset_name = "winogrande" if not tiny else 'tinyBenchmarks/tinyWinogrande'

        if not streaming and not tiny:
            raise ValueError("Loading the full WinoGrande dataset, for speed use streaming=True or tiny=True.")

        if self.question_format is None:
            self.question_format = DEFAULT_WINOGRANDE_QUESTION_FORMAT
            
        def winogrande_map_fn(examples):
            questions = []
            answers = []
            for i in range(len(examples["sentence"])):
                question = self.question_format.format(
                    question=examples["sentence"][i],
                    choice_A=examples["option1"][i],
                    choice_B=examples["option2"][i],
                )
                questions.append(question)
                answers.append(number_to_letter(int(examples["answer"][i])))
            return {
                "question": questions,
                "answer": answers
            }
        
        self.dataset = datasets.load_dataset(
            dataset_name, 
            "winogrande_xl",
            streaming=streaming,
            split="validation",
        )
        self.dataset = self.dataset.map(
            winogrande_map_fn,
            batched=True,
            remove_columns=set(self.dataset.column_names) - {"question", "answer"}
        )
        self.dataset = self.dataset.shuffle(seed=42)





class LambadaTask(MultipleChoiceQuestion):

    def __init__(
        self,
        question_format=None,
        streaming=True,
        max_new_tokens=3,
    ):
        super().__init__(question_format=question_format)
        self.max_new_tokens = max_new_tokens

        if self.question_format is None:
            self.question_format = DEFAULT_LAMBADA_QUESTION_FORMAT
        
        def lambada_map_fn(examples):

            questions = []
            answers = []
            for i in range(len(examples["text"])):
                text = self.question_format.format(
                    question=examples["text"][i]
                ).strip()
                answer = text.split(" ")[-1]
                question = " ".join(text.split(" ")[:-1])
                questions.append(question)
                answers.append(answer)
            return {
                "question": questions,
                "answer": answers
            }
        
        self.dataset = datasets.load_dataset(
            "lambada",
            streaming=streaming,
            split="test",
        )
        self.dataset = self.dataset.map(
            lambada_map_fn,
            batched=True,
            remove_columns=set(self.dataset.column_names) - {"question", "answer"}
        )
        self.dataset = self.dataset.shuffle(seed=42)


class PIQATask(MultipleChoiceQuestion):

    def __init__(
        self,
        question_format=None,
        streaming=True,
    ):
        super().__init__(question_format=question_format)

        if self.question_format is None:
            self.question_format = DEFAULT_2_QUESTION_FORMAT

        def piqa_map_fn(examples):
            questions = []
            answers = []
            for i in range(len(examples["goal"])):
                question = self.question_format.format(
                    question=examples["goal"][i],
                    choice_A=examples["sol1"][i],
                    choice_B=examples["sol2"][i],
                )
                questions.append(question)
                answers.append(number_to_letter(int(examples["label"][i])))
            return {
                "question": questions,
                "answer": answers
            }

        self.dataset = datasets.load_dataset(
            "piqa",
            streaming=streaming,
            split="validation",
        )
        self.dataset = self.dataset.map(
            piqa_map_fn,
            batched=True,
            remove_columns=set(self.dataset.column_names) - {"question", "answer"}
        )
        self.dataset = self.dataset.shuffle(seed=42)


class WMDPTask(MultipleChoiceQuestion):

    def __init__(
        self,
        dataset_name: str,
        question_format=None,
        streaming=True,
    ):
        """
        Built default template for quwstion evaluations into tasks dataset.
        Do not pass in question format unless needs to be different to CUT paper.

        Args:
            dataset_name: Which WMDP dataset you are using.
        """
        super().__init__(question_format=question_format)

        if self.question_format is None:
            self.question_format = DEFAULT_WMDP_QUESTION_FORMAT

        self.name_dict = {"bio": "biology", "cyber": "cybersecurity", "chem": "chemistry"}
        assert dataset_name in self.name_dict.keys(), "Dataset name must be one of 'bio', 'cyber', 'chem'."

        def wmdp_map_fn(examples):
            questions = []
            answers = []

            for i in range(len(examples["question"])):
                question = self.question_format.format(
                    topic=self.name_dict.get(dataset_name),
                    question=examples["question"][i],
                    a1=examples["choices"][i][0],
                    a2=examples["choices"][i][1],
                    a3=examples["choices"][i][2],
                    a4=examples["choices"][i][3],
                )
            questions.append(question)
            answers.append(number_to_letter(int(examples["answer"][i])))
            return {
                "question": questions,
                "answer": answers
            }

        self.dataset = datasets.load_dataset(
            "cais/wmdp",
            name=f"wmdp-{dataset_name}",
            streaming=streaming,
            split="test",
        )
        self.dataset = self.dataset.map(
            wmdp_map_fn,
            batched=True,
            remove_columns=set(self.dataset.column_names) - {"question", "answer"}
        )
        self.dataset = self.dataset.shuffle(seed=42)
'''
