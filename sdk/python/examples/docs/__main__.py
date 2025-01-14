#!/usr/bin/env python
# pylint: disable=too-many-lines, disable=invalid-name

import asyncio
import hashlib
import json
import shutil
import tempfile
import time
from binascii import hexlify, unhexlify
from pathlib import Path

import sha3
from aiohttp import ClientSession
from websockets import connect  # pylint: disable=no-name-in-module

from symbolchain.Bip32 import Bip32
from symbolchain.BufferWriter import BufferWriter
from symbolchain.CryptoTypes import Hash256, PrivateKey, PublicKey
from symbolchain.facade.SymbolFacade import SymbolFacade
from symbolchain.sc import Amount, BlockFactory, Height
from symbolchain.symbol.IdGenerator import generate_mosaic_alias_id, generate_mosaic_id, generate_namespace_id
from symbolchain.symbol.Merkle import MerklePart, deserialize_patricia_tree_nodes, prove_merkle, prove_patricia_merkle
from symbolchain.symbol.MessageEncoder import MessageEncoder
from symbolchain.symbol.Metadata import metadata_update_value
from symbolchain.symbol.Network import NetworkTimestamp  # TODO_: should we link this to Facade or Network to avoid direct import?
from symbolchain.symbol.VotingKeysGenerator import VotingKeysGenerator

SYMBOL_API_ENDPOINT = 'https://reference.symboltest.net:3001'
SYMBOL_WEBSOCKET_ENDPOINT = 'wss://reference.symboltest.net:3001/ws'
SYMBOL_TOOLS_ENDPOINT = 'https://testnet.symbol.tools'
SYMBOL_EXPLORER_TRANSACTION_URL_PATTERN = 'https://testnet.symbol.fyi/transactions/{}'


# region utilities

async def wait_for_transaction_status(transaction_hash, desired_status, **kwargs):
	transaction_description = kwargs.get('transaction_description', 'transaction')
	async with ClientSession(raise_for_status=False) as session:
		for _ in range(600):
			# query the status of the transaction
			async with session.get(f'{SYMBOL_API_ENDPOINT}/transactionStatus/{transaction_hash}') as response:
				# wait for the (JSON) response
				response_json = await response.json()

				# check if the transaction has transitioned
				if 200 == response.status:
					status = response_json['group']
					print(f'{transaction_description} {transaction_hash} has status "{status}"')
					if desired_status == status:
						explorer_url = SYMBOL_EXPLORER_TRANSACTION_URL_PATTERN.format(transaction_hash)
						print(f'{transaction_description} has transitioned to {desired_status}: {explorer_url}')
						return

					if 'failed' == status:
						print(f'{transaction_description} failed validation: {response_json["code"]}')
						break
				else:
					print(f'{transaction_description} {transaction_hash} has unknown status')

			# if not, wait 20s before trying again
			time.sleep(20)

		# fail if the transaction didn't transition to the desired status after 10m
		raise RuntimeError(f'{transaction_description} {transaction_hash} did not transition to {desired_status} in alloted time period')


async def create_account_with_tokens_from_faucet(facade, amount=500, private_key=None):
	# create a key pair that will be used to send transactions
	# when the PrivateKey is known, pass the raw private key bytes or hex encoded string to the PrivateKey(...) constructor instead
	key_pair = facade.KeyPair(PrivateKey.random()) if private_key is None else facade.KeyPair(private_key)
	address = facade.network.public_key_to_address(key_pair.public_key)
	print(f'new account created with address: {address}')

	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP POST request to faucet endpoint
		request = {
			'recipient': str(address),
			'amount': amount,
			'selectedMosaics': ['72C0212E67A08BCE']  # XYM mosaic id on testnet
		}
		async with session.post(f'{SYMBOL_TOOLS_ENDPOINT}/claims', json=request) as response:
			# wait for the (JSON) response
			response_json = await response.json()

			# extract the funding transaction hash and wait for it to be confirmed
			transaction_hash = Hash256(response_json['txHash'])
			await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='funding transaction')

	return key_pair

# endregion


# region create empty account

def create_random_account(facade):
	# create a signing key pair that will be associated with an account
	key_pair = facade.KeyPair(PrivateKey.random())

	# convert the public key to a network-dependent address (unique account identifier)
	address = facade.network.public_key_to_address(key_pair.public_key)

	# output account public and private details
	print(f'    address: {address}')
	print(f' public key: {key_pair.public_key}')
	print(f'private key: {key_pair.private_key}')


def create_random_bip32_account(facade):
	# create a random Bip39 seed phrase (mnemonic)
	bip32 = Bip32()
	mnemonic = bip32.random()

	# derive a root Bip32 node from the mnemonic and a password 'correcthorsebatterystaple'
	root_node = bip32.from_mnemonic(mnemonic, 'correcthorsebatterystaple')

	# derive a child Bip32 node from the root Bip32 node for the account at index 0
	child_node = root_node.derive_path(facade.bip32_path(0))

	# convert the Bip32 node to a signing key pair
	key_pair = facade.bip32_node_to_key_pair(child_node)

	# convert the public key to a network-dependent address (unique account identifier)
	address = facade.network.public_key_to_address(key_pair.public_key)

	# output account public and private details
	print(f'   mnemonic: {mnemonic}')
	print(f'    address: {address}')
	print(f' public key: {key_pair.public_key}')
	print(f'private key: {key_pair.private_key}')

# endregion


# region voting key file generation

def create_voting_key_file(facade):
	# create a voting key pair
	voting_key_pair = facade.KeyPair(PrivateKey.random())

	# create a file generator
	generator = VotingKeysGenerator(voting_key_pair)

	# generate voting key file for epochs 10-150
	buffer = generator.generate(10, 150)

	# store to file
	# note: additional care should be taken to create file with proper permissions
	with tempfile.TemporaryDirectory() as temp_directory:
		with open(Path(temp_directory) / 'private_key_tree1.dat', 'wb') as output_file:
			output_file.write(buffer)

	# show voting key public key
	print(f'voting key public key {voting_key_pair.public_key}')

# endregion


# region network property accessors

async def get_network_time():
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP GET request to a Symbol REST endpoint
		async with session.get(f'{SYMBOL_API_ENDPOINT}/node/time') as response:
			# wait for the (JSON) response
			response_json = await response.json()

			# extract the network time from the json
			timestamp = NetworkTimestamp(int(response_json['communicationTimestamps']['receiveTimestamp']))
			print(f'network time: {timestamp} ms')
			return timestamp


async def get_maximum_supply():
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP GET request to a Symbol REST endpoint
		async with session.get(f'{SYMBOL_API_ENDPOINT}/network/currency/supply/max') as response:
			# wait for the (text) response and interpret it as a floating point value
			maximum_supply = float(await response.text())
			print(f'maximum supply: {maximum_supply:.6f} XYM')
			return maximum_supply


async def get_total_supply():
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP GET request to a Symbol REST endpoint
		async with session.get(f'{SYMBOL_API_ENDPOINT}/network/currency/supply/total') as response:
			# wait for the (text) response and interpret it as a floating point value
			total_supply = float(await response.text())
			print(f'total supply: {total_supply:.6f} XYM')
			return total_supply


async def get_circulating_supply():
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP GET request to a Symbol REST endpoint
		async with session.get(f'{SYMBOL_API_ENDPOINT}/network/currency/supply/circulating') as response:
			# wait for the (text) response and interpret it as a floating point value
			circulating_supply = float(await response.text())
			print(f'circulating supply: {circulating_supply:.6f} XYM')
			return circulating_supply


async def get_network_height():
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP GET request to a Symbol REST endpoint
		async with session.get(f'{SYMBOL_API_ENDPOINT}/chain/info') as response:
			# wait for the (JSON) response
			response_json = await response.json()

			# extract the height from the json
			height = int(response_json['height'])
			print(f'height: {height}')
			return height


async def get_network_finalized_height():
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP GET request to a Symbol REST endpoint
		async with session.get(f'{SYMBOL_API_ENDPOINT}/chain/info') as response:
			# wait for the (JSON) response
			response_json = await response.json()

			# extract the finalized height from the json
			height = int(response_json['latestFinalizedBlock']['height'])
			print(f'finalized height: {height}')
			return height

# endregion


# region block files

class BlockDigester:
	OBJECTS_PER_STORAGE_DIRECTORY = 10_000

	def __init__(self, data_path, file_database_batch_size):
		self.data_path = data_path
		self.file_database_batch_size = file_database_batch_size

		# used to cache last opened batch file
		self.batch_file_path = None
		self.batch_file_data = None
		self.block_offsets = None

		self.end = 0

	def get_chain_height(self):
		"""Gets chain height."""

		with open(self.data_path / 'index.dat', 'rb') as input_file:
			data = input_file.read()

		return Height.deserialize(data)

	def parse_offsets(self, ignore_first_zero_offset=False):
		"""There are absolute offsets to blocks at the beginning of the file, parse and store them."""

		buffer = memoryview(self.batch_file_data)

		self.block_offsets = []
		for i in range(self.file_database_batch_size):
			offset = int.from_bytes(buffer[:8], byteorder='little', signed=False)

			# if this is very first batch file it will have 0 as a first entry
			# for any other batch file, 0 means all offsets have been read
			if not offset and not ignore_first_zero_offset and i == 0:
				break

			self.block_offsets.append(offset)
			buffer = buffer[8:]

	def read_batchfile(self, height, force_reread):
		"""Open proper block batch file and parse offsets."""

		group_id = (height // self.file_database_batch_size) * self.file_database_batch_size

		directory = f'{group_id // self.OBJECTS_PER_STORAGE_DIRECTORY:05}'
		name = f'{group_id % self.OBJECTS_PER_STORAGE_DIRECTORY:05}.dat'

		file_path = self.data_path / directory / name

		if self.batch_file_path == file_path and not force_reread:
			return

		with file_path.open(mode='rb', buffering=0) as input_file:
			self.batch_file_data = input_file.read()
			self.batch_file_path = file_path

			self.parse_offsets(group_id == 0)

	def get_block(self, height, force_reread=False):
		"""Returns parsed block at height."""

		self.read_batchfile(height, force_reread)

		entry_in_batch = height % self.file_database_batch_size
		if entry_in_batch >= len(self.block_offsets):
			raise RuntimeError(f'block with given height ({height}) is not present in batch file')

		offset = self.block_offsets[entry_in_batch]
		return BlockFactory.deserialize(self.batch_file_data[offset:])

# endregion


# region account property accessors

async def get_network_currency():
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP GET request to a Symbol REST endpoint
		async with session.get(f'{SYMBOL_API_ENDPOINT}/network/properties') as response:
			# wait for the (JSON) response
			properties = await response.json()

			# exctract currency mosaic id
			mosaic_id = int(properties['chain']['currencyMosaicId'].replace('\'', ''), 0)
			print(f'currency mosaic id: {mosaic_id}')
			return mosaic_id


async def get_mosaic_properties(mosaic_id):
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP GET request to a Symbol REST endpoint
		async with session.get(f'{SYMBOL_API_ENDPOINT}/mosaics/{mosaic_id}') as response:
			# wait for the (JSON) response
			return await response.json()


async def get_account_state():
	account_identifier = 'TA4RYHMNHCFRCT2PCWOCJMWVAQ3ZCJDOTF2SGBI'  # Address or public key

	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP GET request to a Symbol REST endpoint
		async with session.get(f'{SYMBOL_API_ENDPOINT}/accounts/{account_identifier}') as response:
			# wait for the (JSON) response
			return await response.json()


async def get_account_balance():
	network_currency_id = await get_network_currency()
	network_currency_id_formatted = f'{network_currency_id:08X}'

	currency_mosaic = await get_mosaic_properties(network_currency_id_formatted)
	divisibility = currency_mosaic['mosaic']['divisibility']

	account_state = await get_account_state()

	# search for currency inside account mosaics
	account_currency = next(mosaic for mosaic in account_state['account']['mosaics'] if network_currency_id == int(mosaic['id'], 16))
	amount = int(account_currency['amount'])
	account_balance = {
		'balance': {
			'id': account_currency['id'],
			'amount': amount,
			'formatted_amount': f'{amount // 10**divisibility}.{(amount % 10**divisibility):0{divisibility}}'
		}
	}

	print(account_balance)
	return account_balance

# endregion


# region transfer transactions

def decrypt_utf8_message(key_pair, public_key, encrypted_payload):
	message_encoder = MessageEncoder(key_pair)
	(is_decode_success, plain_message) = message_encoder.try_decode(public_key, encrypted_payload)
	if is_decode_success:
		print(f'decrypted message: {plain_message.decode("utf8")}')
	else:
		print(f'unable to decrypt message: {hexlify(encrypted_payload)}')


async def create_transfer_with_encrypted_message(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic recipient (it insecurely deterministically generated for the benefit of related tests)
	recipient_key_pair = facade.KeyPair(PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, 0])))
	recipient_address = facade.network.public_key_to_address(recipient_key_pair.public_key)
	print(f'recipient: {recipient_address}')

	# encrypt a message using a signer's private key and recipient's public key
	message_encoder = MessageEncoder(signer_key_pair)
	encrypted_payload = message_encoder.encode(recipient_key_pair.public_key, 'this is a secret message'.encode('utf8'))
	print(f'encrypted message: {hexlify(encrypted_payload)}')

	# the same encoder can be used to decode a message
	decrypt_utf8_message(signer_key_pair, recipient_key_pair.public_key, encrypted_payload)

	# alternatively, an encoder around the recipient private key and signer public key can decode the message too
	decrypt_utf8_message(recipient_key_pair, signer_key_pair.public_key, encrypted_payload)

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'transfer_transaction_v1',
		'recipient_address': recipient_address,
		'mosaics': [
			{'mosaic_id': generate_mosaic_alias_id('symbol.xym'), 'amount': 7_000000},  # send 7 of XYM to recipient
		],
		'message': encrypted_payload
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'transfer transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='transfer transaction')


# endregion


# region account transactions

async def create_account_metadata_new(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# metadata transaction needs to be wrapped in aggregate transaction

	value = 'https://twitter.com/NCOSIGIMCITYNREmalformed'.encode('utf8')

	embedded_transactions = [
		facade.transaction_factory.create_embedded({
			'type': 'account_metadata_transaction_v1',
			'signer_public_key': signer_key_pair.public_key,

			# the key consists of a tuple (signer, target_address, scoped_metadata_key)
			#  - if signer is different than target address, the target account will need to cosign the transaction
			#  - scoped_metadata_key can be any 64-bit value picked by metadata creator
			'target_address': facade.network.public_key_to_address(signer_key_pair.public_key),
			'scoped_metadata_key': 0x72657474697774,

			'value_size_delta': len(value),  # when creating _new_ value this needs to be equal to value size
			'value': value
		})
	]
	# create the transaction
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'aggregate_complete_transaction_v2',
		'transactions_hash': facade.hash_embedded_transactions(embedded_transactions),
		'transactions': embedded_transactions
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'account metadata (new) transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='account metadata (new) transaction')


async def create_account_metadata_modify(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# metadata transaction needs to be wrapped in aggregate transaction

	# to update existing metadata, new value needs to be 'xored' with previous value.
	old_value = 'https://twitter.com/NCOSIGIMCITYNREmalformed'.encode('utf8')
	new_value = 'https://twitter.com/0x6861746366574'.encode('utf8')
	update_value = metadata_update_value(old_value, new_value)

	embedded_transactions = [
		facade.transaction_factory.create_embedded({
			'type': 'account_metadata_transaction_v1',
			'signer_public_key': signer_key_pair.public_key,

			# the key consists of a tuple (signer, target_address, scoped_metadata_key),
			# when updating all values must match previously used values
			'target_address': facade.network.public_key_to_address(signer_key_pair.public_key),
			'scoped_metadata_key': 0x72657474697774,

			'value_size_delta': len(new_value) - len(old_value),  # change in size, negative because the value will be shrunk
			'value': update_value
		})
	]
	# create the transaction
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'aggregate_complete_transaction_v2',
		'transactions_hash': facade.hash_embedded_transactions(embedded_transactions),
		'transactions': embedded_transactions
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'account metadata (modify) transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='account metadata (modify) transaction')


# endregion


# region secret lock transaction

async def create_secret_lock(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic recipient (it insecurely deterministically generated for the benefit of related tests)
	recipient_address = facade.network.public_key_to_address(PublicKey(signer_key_pair.public_key.bytes[:-4] + bytes([0, 0, 0, 0])))
	print(f'recipient: {recipient_address}')

	# double sha256 hash the proof value
	secret_hash = Hash256(hashlib.sha256(hashlib.sha256('correct horse battery staple'.encode('utf8')).digest()).digest())

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'secret_lock_transaction_v1',
		'mosaic': {'mosaic_id': generate_mosaic_alias_id('symbol.xym'), 'amount': 7_000000},  # mosaic to transfer upon proof

		'duration': 111,  # number of blocks
		'recipient_address': recipient_address,
		'secret': secret_hash,
		'hash_algorithm': 'hash_256'  # double Hash256
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'secret lock transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='secret lock transaction')


async def create_secret_proof(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic recipient (it insecurely deterministically generated for the benefit of related tests)
	recipient_address = facade.network.public_key_to_address(PublicKey(signer_key_pair.public_key.bytes[:-4] + bytes([0, 0, 0, 0])))
	print(f'recipient: {recipient_address}')

	# double sha256 hash the proof value
	secret_hash = Hash256(hashlib.sha256(hashlib.sha256('correct horse battery staple'.encode('utf8')).digest()).digest())

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'secret_proof_transaction_v1',

		'recipient_address': recipient_address,
		'secret': secret_hash,
		'hash_algorithm': 'hash_256',  # double Hash256
		'proof': 'correct horse battery staple'
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'secret proof transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='secret proof transaction')


# endregion


# region namespace transactions

async def create_namespace_registration_root(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create the transaction
	namespace_name = f'who_{str(signer_address).lower()}'
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'namespace_registration_transaction_v1',
		'registration_type': 'root',  # 'root' indicates a root namespace is being created
		'duration': 86400,  # number of blocks the root namespace will be active; approximately 30 (86400 / 2880) days
		'name': namespace_name  # name of the root namespace
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'namespace (root) registration transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='namespace (root) registration transaction')


async def create_namespace_registration_child(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create the transaction
	root_namespace_name = f'who_{str(signer_address).lower()}'
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'namespace_registration_transaction_v1',
		'registration_type': 'child',  # 'child' indicates a namespace will be attach to some existing root namespace
		'parent_id': generate_namespace_id(root_namespace_name),  # this points to root namespace
		'name': 'killed'  # name of the child namespace
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'namespace (child) registration transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='namespace (child) registration transaction')


async def create_namespace_metadata_new(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# metadata transaction needs to be wrapped in aggregate transaction

	root_namespace_name = f'who_{str(signer_address).lower()}'
	value = 'Laura Palmer'.encode('utf8')

	embedded_transactions = [
		facade.transaction_factory.create_embedded({
			'type': 'namespace_metadata_transaction_v1',
			'signer_public_key': signer_key_pair.public_key,

			# the key consists of a tuple (signer, target_address, target_namespace_id, scoped_metadata_key)
			#  - if signer is different than target address, the target account will need to cosign the transaction
			#  - target address must be namespace owner
			#  - namespace with target_namespace_id must exist
			#  - scoped_metadata_key can be any 64-bit value picked by metadata creator
			'target_address': facade.network.public_key_to_address(signer_key_pair.public_key),
			'target_namespace_id': generate_namespace_id('killed', generate_namespace_id(root_namespace_name)),
			'scoped_metadata_key': int.from_bytes(b'name', byteorder='little'),

			'value_size_delta': len(value),
			'value': value
		})
	]
	# create the transaction
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'aggregate_complete_transaction_v2',
		'transactions_hash': facade.hash_embedded_transactions(embedded_transactions),
		'transactions': embedded_transactions
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'namespace metadata (new) transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='namespace metadata (new) transaction')


async def create_namespace_metadata_modify(facade, signer_key_pair):  # pylint: disable=too-many-locals
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# metadata transaction needs to be wrapped in aggregate transaction

	root_namespace_name = f'who_{str(signer_address).lower()}'
	old_value = 'Laura Palmer'.encode('utf8')
	new_value = 'Catherine Martell'.encode('utf8')
	update_value = metadata_update_value(old_value, new_value)

	embedded_transactions = [
		facade.transaction_factory.create_embedded({
			'type': 'namespace_metadata_transaction_v1',
			'signer_public_key': signer_key_pair.public_key,

			# the key consists of a tuple (signer, target_address, target_namespace_id, scoped_metadata_key)
			# when updating all values must match previously used values
			'target_address': facade.network.public_key_to_address(signer_key_pair.public_key),
			'target_namespace_id': generate_namespace_id('killed', generate_namespace_id(root_namespace_name)),
			'scoped_metadata_key': int.from_bytes(b'name', byteorder='little'),

			'value_size_delta': len(new_value) - len(old_value),
			'value': update_value
		})
	]
	# create the transaction
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'aggregate_complete_transaction_v2',
		'transactions_hash': facade.hash_embedded_transactions(embedded_transactions),
		'transactions': embedded_transactions
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'namespace metadata (modify) transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='namespace metadata (modify) transaction')


# endregion


# region mosaic transactions

async def create_mosaic_definition_new(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'mosaic_definition_transaction_v1',
		'duration': 0,  # number of blocks the mosaic will be active; 0 indicates it will never expire
		'divisibility': 2,  # number of supported decimal places

		# nonce is used as a locally unique identifier for mosaics with a common owner
		# mosaic id is derived from the owner's address and the nonce
		'nonce': 123,

		# set of restrictions to apply to the mosaic
		# - 'transferable' indicates the mosaic can be freely transfered among any account that can own the mosaic
		# - 'restrictable' indicates that the owner can restrict the accounts that can own the mosaic
		'flags': 'transferable restrictable'
	})

	# transaction.id field is mosaic id and it is filled automatically after calling transaction_factory.create()

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'mosaic definition transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='mosaic definition transaction')


async def create_mosaic_definition_modify(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a transaction that modifies an existing mosaic definition
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'mosaic_definition_transaction_v1',
		'duration': 0,  # number of blocks the mosaic will be active; 0 indicates it will never expire (added to existing value: 0 + 0 = 0)
		'divisibility': 0,  # number of supported decimal places (XOR'd against existing value: 2 ^ 0 = 2)

		# nonce is used as a locally unique identifier for mosaics with a common owner and identifies the mosaic definition to modify
		# mosaic id is derived from the owner's address and the nonce
		'nonce': 123,

		# set of restrictions to apply to the mosaic
		# (XOR'd against existing value: (transferable|restrictable) ^ revokable = transferable|restrictable|revokable)
		# - 'revokable' indicates the mosaic can be revoked by the owner from any account
		'flags': 'revokable'
	})

	# transaction.id field is mosaic id and it is filled automatically after calling transaction_factory.create()

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'mosaic definition transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='mosaic definition transaction')


async def create_mosaic_supply(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'mosaic_supply_change_transaction_v1',
		'mosaic_id': generate_mosaic_id(signer_address, 123),

		# action can either be 'increase' or 'decrease',
		# if mosaic does not have 'mutable supply' flag, owner can issue supply change transactions only if owns full supply
		'action': 'increase',

		# delta is always unsigned number, it's specified in atomic units, created mosaic has divisibility set to 2 decimal places,
		# so following delta will result in 100 units
		'delta': 100_00
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'mosaic supply transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='mosaic supply transaction')


async def create_mosaic_transfer(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic recipient (it insecurely deterministically generated for the benefit of related tests)
	recipient_address = facade.network.public_key_to_address(PublicKey(signer_key_pair.public_key.bytes[:-4] + bytes([0, 0, 0, 0])))
	print(f'recipient: {recipient_address}')

	# send 10 of the custom mosaic to the recipient
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'transfer_transaction_v1',
		'recipient_address': recipient_address,
		'mosaics': [
			{'mosaic_id': generate_mosaic_id(signer_address, 123), 'amount': 10_00}
		]
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'mosaic transfer transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='mosaic transfer transaction')


async def create_mosaic_revocation(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic source (it insecurely deterministically generated for the benefit of related tests)
	source_address = facade.network.public_key_to_address(PublicKey(signer_key_pair.public_key.bytes[:-4] + bytes([0, 0, 0, 0])))
	print(f'source: {source_address}')

	# revoke 7 of the custom mosaic from the recipient
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'mosaic_supply_revocation_transaction_v1',
		'source_address': source_address,
		'mosaic': {'mosaic_id': generate_mosaic_id(signer_address, 123), 'amount': 7_00}
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'mosaic revocation transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='mosaic revocation transaction')


async def create_mosaic_atomic_swap(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a second signing key pair that will be used as the swap partner
	partner_key_pair = await create_account_with_tokens_from_faucet(facade)

	# Alice (signer) owns some amount of custom mosaic (with divisibility=2)
	# Bob (partner) wants to exchange 20 xym for a single piece of Alice's custom mosaic
	# there will be two transfers within an aggregate
	embedded_transactions = [
		facade.transaction_factory.create_embedded({
			'type': 'transfer_transaction_v1',
			'signer_public_key': partner_key_pair.public_key,

			'recipient_address': facade.network.public_key_to_address(signer_key_pair.public_key),
			'mosaics': [
				{'mosaic_id': generate_mosaic_alias_id('symbol.xym'), 'amount': 20_000000}
			]
		}),

		facade.transaction_factory.create_embedded({
			'type': 'transfer_transaction_v1',
			'signer_public_key': signer_key_pair.public_key,

			'recipient_address': facade.network.public_key_to_address(partner_key_pair.public_key),
			'mosaics': [
				{'mosaic_id': generate_mosaic_id(signer_address, 123), 'amount': 100}
			]
		})
	]

	# Alice will be signer of aggregate itself, that also means he won't have to attach his cosignature
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'aggregate_complete_transaction_v2',
		'transactions_hash': facade.hash_embedded_transactions(embedded_transactions),
		'transactions': embedded_transactions
	})

	# Bob needs to cosign the transaction because the swap will only be confirmed if both the sender and the partner agree to it

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	# when setting the fee for an aggregate complete, include the size of cosignatures (added later) in the fee calculation
	transaction.fee = Amount(100 * (transaction.size + len([partner_key_pair]) * 104))

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'mosaic swap transaction hash {transaction_hash}')

	# cosign transaction by all partners (this is dependent on the hash and consequently the main signature)
	for cosignatory_key_pair in [partner_key_pair]:
		cosignature = facade.cosign_transaction(cosignatory_key_pair, transaction)
		transaction.cosignatures.append(cosignature)

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='mosaic swap transaction')


# endregion


# region mosaic metadata

async def create_mosaic_metadata_new(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# metadata transaction needs to be wrapped in aggregate transaction

	value = unhexlify(
		'89504e470d0a1a0a0000000d49484452000000010000000108000000003a7e9b55'
		'0000000a49444154185763f80f00010101005a4d6ff10000000049454e44ae426082')

	embedded_transactions = [
		facade.transaction_factory.create_embedded({
			'type': 'mosaic_metadata_transaction_v1',
			'signer_public_key': signer_key_pair.public_key,

			# the key consists of a tuple (signer, target_address, target_mosaic_id, scoped_metadata_key)
			#  - if signer is different than target address, the target account will need to cosign the transaction
			#  - target address must be mosaic owner
			#  - mosaic with target_mosaic_id must exist
			#  - scoped_metadata_key can be any 64-bit value picked by metadata creator
			'target_address': signer_address,
			'target_mosaic_id': generate_mosaic_id(signer_address, 123),
			'scoped_metadata_key': int.from_bytes(b'avatar', byteorder='little'),  # this can be any 64-bit value picked by creator

			'value_size_delta': len(value),  # when creating _new_ value this needs to be equal to value size
			'value': value
		})
	]
	# create the transaction
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'aggregate_complete_transaction_v2',
		'transactions_hash': facade.hash_embedded_transactions(embedded_transactions),
		'transactions': embedded_transactions
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'mosaic metadata (new) transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='mosaic metadata (new) transaction')


async def create_mosaic_metadata_cosigned_1(facade, signer_key_pair):
	# pylint: disable=too-many-locals
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	authority_semi_deterministic_key = PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, 0]))
	authority_key_pair = await create_account_with_tokens_from_faucet(facade, 100, authority_semi_deterministic_key)

	# set new high score for an account

	value = (440).to_bytes(4, byteorder='little')

	embedded_transactions = [
		facade.transaction_factory.create_embedded({
			'type': 'mosaic_metadata_transaction_v1',
			'signer_public_key': authority_key_pair.public_key,

			# the key consists of a tuple (signer, target_address, target_mosaic_id, scoped_metadata_key)
			#  - if signer is different than target address, the target account will need to cosign the transaction
			#  - target address must be mosaic owner
			#  - mosaic with target_mosaic_id must exist
			#  - scoped_metadata_key can be any 64-bit value picked by metadata creator
			'target_mosaic_id': generate_mosaic_id(signer_address, 123),
			'scoped_metadata_key': int.from_bytes(b'rating', byteorder='little'),  # this can be any 64-bit value picked by creator
			'target_address': signer_address,

			'value_size_delta': len(value),  # when creating _new_ value this needs to be equal to value size
			'value': value
		})
	]
	# create the transaction
	transaction = facade.transaction_factory.create({
		'signer_public_key': authority_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'aggregate_complete_transaction_v2',
		'transactions_hash': facade.hash_embedded_transactions(embedded_transactions),
		'transactions': embedded_transactions
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	# when setting the fee for an aggregate complete, include the size of cosignatures (added later) in the fee calculation
	transaction.fee = Amount(100 * (transaction.size + len([signer_key_pair]) * 104))

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(authority_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'mosaic metadata (cosigned 1) transaction hash {transaction_hash}')

	# cosign transaction by all partners (this is dependent on the hash and consequently the main signature)
	for cosignatory_key_pair in [signer_key_pair]:
		cosignature = facade.cosign_transaction(cosignatory_key_pair, transaction)
		transaction.cosignatures.append(cosignature)

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='mosaic metadata (cosigned 1) transaction')


async def create_mosaic_metadata_cosigned_2(facade, signer_key_pair):
	# pylint: disable=too-many-locals
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	authority_semi_deterministic_key = PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, 0]))
	authority_key_pair = await create_account_with_tokens_from_faucet(facade, 100, authority_semi_deterministic_key)

	# update high score for an account

	old_value = (440).to_bytes(4, byteorder='little')
	new_value = (9001).to_bytes(4, byteorder='little')
	update_value = metadata_update_value(old_value, new_value)

	embedded_transactions = [
		facade.transaction_factory.create_embedded({
			'type': 'mosaic_metadata_transaction_v1',
			'signer_public_key': authority_key_pair.public_key,

			# the key consists of a tuple (signer, target_address, target_mosaic_id, scoped_metadata_key)
			# when updating all values must match previously used values
			'target_mosaic_id': generate_mosaic_id(signer_address, 123),
			'scoped_metadata_key': int.from_bytes(b'rating', byteorder='little'),  # this can be any 64-bit value picked by creator
			'target_address': signer_address,

			# this should be difference between sizes, but this example does not change the size, so delta = 0
			'value_size_delta': 0,
			'value': update_value
		})
	]
	# create the transaction
	transaction = facade.transaction_factory.create({
		'signer_public_key': authority_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'aggregate_complete_transaction_v2',
		'transactions_hash': facade.hash_embedded_transactions(embedded_transactions),
		'transactions': embedded_transactions
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	# when setting the fee for an aggregate complete, include the size of cosignatures (added later) in the fee calculation
	transaction.fee = Amount(100 * (transaction.size + len([signer_key_pair]) * 104))

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(authority_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'mosaic metadata (cosigned 2) transaction hash {transaction_hash}')

	# cosign transaction by all partners (this is dependent on the hash and consequently the main signature)
	for cosignatory_key_pair in [signer_key_pair]:
		cosignature = facade.cosign_transaction(cosignatory_key_pair, transaction)
		transaction.cosignatures.append(cosignature)

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='mosaic metadata (cosigned 2) transaction')


# endregion


# region mosaic restrictions transactions

async def create_global_mosaic_restriction_new(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'mosaic_global_restriction_transaction_v1',
		'mosaic_id': generate_mosaic_id(signer_address, 123),

		# restriction might use some other mosaic restriction rules, that mosaic doesn't even have to belong to current owner
		'reference_mosaic_id': 0,
		'restriction_key': 0xC0FFE,
		'previous_restriction_type': 0,  # this is newly created restriction so there was no previous type
		'previous_restriction_value': 0,

		# 'ge' means greater or equal, possible operators are: 'eq', 'ne', 'lt', 'le', 'gt', 'ge'
		'new_restriction_type': 'ge',
		'new_restriction_value': 1
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'global mosaic restriction (new) transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='global mosaic restriction (new) transaction')


async def create_address_mosaic_restriction_1(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'mosaic_address_restriction_transaction_v1',
		'mosaic_id': generate_mosaic_id(signer_address, 123),

		'restriction_key': 0xC0FFE,
		'previous_restriction_value': 0xFFFFFFFF_FFFFFFFF,
		'new_restriction_value': 10,
		'target_address': facade.network.public_key_to_address(signer_key_pair.public_key)
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'address mosaic restriction (new:1) transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='address mosaic restriction (new:1) transaction')


async def create_address_mosaic_restriction_2(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'mosaic_address_restriction_transaction_v1',
		'mosaic_id': generate_mosaic_id(signer_address, 123),

		'restriction_key': 0xC0FFE,
		'previous_restriction_value': 0xFFFFFFFF_FFFFFFFF,
		'new_restriction_value': 1,
		'target_address': SymbolFacade.Address('TBOBBYKOYQBWK3HSX7NQVJ5JFPE22352AVDXXAA')
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'address mosaic restriction (new:2) transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='address mosaic restriction (new:2) transaction')


async def create_address_mosaic_restriction_3(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'mosaic_address_restriction_transaction_v1',
		'mosaic_id': generate_mosaic_id(signer_address, 123),

		'restriction_key': 0xC0FFE,
		'previous_restriction_value': 0xFFFFFFFF_FFFFFFFF,
		'new_restriction_value': 2,
		'target_address': SymbolFacade.Address('TALICECI35BNIJQA5CNUKI2DY3SXNEHPZJSOVAA')
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'address mosaic restriction (new:3) transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='address mosaic restriction (new:3) transaction')


async def create_global_mosaic_restriction_modify(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'mosaic_global_restriction_transaction_v1',
		'mosaic_id': generate_mosaic_id(signer_address, 123),

		'reference_mosaic_id': 0,
		'restriction_key': 0xC0FFE,
		'previous_restriction_type': 'ge',  # must match old restriction type
		'previous_restriction_value': 1,  # must match old restriction value

		'new_restriction_type': 'ge',
		'new_restriction_value': 2
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'global mosaic restriction (modify) transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='global mosaic restriction (modify) transaction')


# endregion


# region links

async def create_account_key_link(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic remote account (insecurely deterministically generated for the benefit of related tests)
	# this account will sign blocks on behalf of the (funded) signing account
	remote_key_pair = facade.KeyPair(PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, 0])))
	print(f'remote public key: {remote_key_pair.public_key}')

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'account_key_link_transaction_v1',
		'linked_public_key': remote_key_pair.public_key,
		'link_action': 'link'
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'account key link transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='account key link transaction')


async def create_vrf_key_link(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic VRF account (insecurely deterministically generated for the benefit of related tests)
	# this account will inject randomness into blocks harvested by the (funded) signing account
	vrf_key_pair = facade.KeyPair(PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, 0])))
	print(f'VRF public key: {vrf_key_pair.public_key}')

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'vrf_key_link_transaction_v1',
		'linked_public_key': vrf_key_pair.public_key,
		'link_action': 'link'
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'vrf key link transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='vrf key link transaction')


async def create_voting_key_link(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic root voting public key (insecurely deterministically generated for the benefit of related tests)
	# this account will be participate in voting on behalf of the (funded) signing account
	voting_public_key = PublicKey(signer_key_pair.public_key.bytes[:-4] + bytes([0, 0, 0, 0]))
	print(f'voting public key: {voting_public_key}')

	# notice that voting changes will only take effect after finalization of the block containing the voting key link transaction
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'voting_key_link_transaction_v1',
		'linked_public_key': voting_public_key,
		'start_epoch': 10,
		'end_epoch': 150,
		'link_action': 'link'
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'node key link transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='node key link transaction')


async def create_node_key_link(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic node public key (insecurely deterministically generated for the benefit of related tests)
	# this account will be asked to host delegated harvesting of the (funded) signing account
	node_public_key = PublicKey(signer_key_pair.public_key.bytes[:-4] + bytes([0, 0, 0, 0]))
	print(f'node public key: {node_public_key}')

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'node_key_link_transaction_v1',
		'linked_public_key': node_public_key,
		'link_action': 'link'
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'node key link transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='node key link transaction')


async def create_harvesting_delegation_message(facade, signer_key_pair):
	# pylint: disable=too-many-locals
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic remote account (insecurely deterministically generated for the benefit of related tests)
	# this account will sign blocks on behalf of the (funded) signing account
	remote_key_pair = facade.KeyPair(PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, 0])))
	print(f'remote public key: {remote_key_pair.public_key}')

	# create a deterministic VRF account (insecurely deterministically generated for the benefit of related tests)
	# this account will inject randomness into blocks harvested by the (funded) signing account
	vrf_key_pair = facade.KeyPair(PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, 1])))
	print(f'VRF public key: {vrf_key_pair.public_key}')

	# create a deterministic node public key (insecurely deterministically generated for the benefit of related tests)
	# this account will be asked to host delegated harvesting of the (funded) signing account
	node_public_key = facade.KeyPair(PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, 2]))).public_key
	print(f'node public key: {node_public_key}')

	# create a harvesting delecation request message using the signer's private key and the remote node's public key
	# the signer's remote and VRF private keys will be shared with the node
	# in order to deactivate, these should be regenerated
	message_encoder = MessageEncoder(signer_key_pair)
	harvest_request_payload = message_encoder.encode_persistent_harvesting_delegation(node_public_key, remote_key_pair, vrf_key_pair)
	print(f'harvest request message: {hexlify(harvest_request_payload)}')

	# the same encoder can be used to decode a message
	decrypt_utf8_message(signer_key_pair, node_public_key, harvest_request_payload)

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'transfer_transaction_v1',
		'recipient_address': facade.network.public_key_to_address(node_public_key),
		'message': harvest_request_payload
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'transfer transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='transfer transaction')


async def create_account_key_unlink(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic remote account (insecurely deterministically generated for the benefit of related tests)
	# this account will sign blocks on behalf of the (funded) signing account
	remote_key_pair = facade.KeyPair(PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, 0])))
	print(f'remote public key: {remote_key_pair.public_key}')

	# when unlinking, linked_public_key must match previous value used in link
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'account_key_link_transaction_v1',
		'linked_public_key': remote_key_pair.public_key,
		'link_action': 'unlink'
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'account key link transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='account key link transaction')


async def create_vrf_key_unlink(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic VRF account (insecurely deterministically generated for the benefit of related tests)
	# this account will inject randomness into blocks harvested by the (funded) signing account
	vrf_key_pair = facade.KeyPair(PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, 0])))
	print(f'VRF public key: {vrf_key_pair.public_key}')

	# when unlinking, linked_public_key must match previous value used in link
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'vrf_key_link_transaction_v1',
		'linked_public_key': vrf_key_pair.public_key,
		'link_action': 'unlink'
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'vrf key link transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='vrf key link transaction')


async def create_voting_key_unlink(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic root voting public key (insecurely deterministically generated for the benefit of related tests)
	# this account will be participate in voting on behalf of the (funded) signing account
	voting_public_key = PublicKey(signer_key_pair.public_key.bytes[:-4] + bytes([0, 0, 0, 0]))
	print(f'voting public key: {voting_public_key}')

	# notice that voting changes will only take effect after finalization of the block containing the voting key link transaction
	# when unlinking, linked_public_key must match previous value used in link
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'voting_key_link_transaction_v1',
		'linked_public_key': voting_public_key,
		'start_epoch': 10,
		'end_epoch': 150,
		'link_action': 'unlink'
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'node key link transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='node key link transaction')


async def create_node_key_unlink(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic node public key (insecurely deterministically generated for the benefit of related tests)
	# this account will be asked to host delegated harvesting of the (funded) signing account
	node_public_key = PublicKey(signer_key_pair.public_key.bytes[:-4] + bytes([0, 0, 0, 0]))
	print(f'node public key: {node_public_key}')

	# when unlinking, linked_public_key must match previous value used in link
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'node_key_link_transaction_v1',
		'linked_public_key': node_public_key,
		'link_action': 'unlink'
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'node key link transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='node key link transaction')


# endregion


# region account multisig management transactions (complete)

async def create_multisig_account_modification_new_account(facade, signer_key_pair):
	# pylint: disable=too-many-locals
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create cosignatory key pairs, where each cosignatory will be required to cosign initial modification
	# (they are insecurely deterministically generated for the benefit of related tests)
	cosignatory_key_pairs = [facade.KeyPair(PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, i]))) for i in range(3)]
	cosignatory_addresses = [facade.network.public_key_to_address(key_pair.public_key) for key_pair in cosignatory_key_pairs]

	# multisig account modification transaction needs to be wrapped in aggregate transaction

	embedded_transactions = [
		facade.transaction_factory.create_embedded({
			'type': 'multisig_account_modification_transaction_v1',
			'signer_public_key': signer_key_pair.public_key,

			'min_approval_delta': 2,  # number of signatures required to make any transaction
			'min_removal_delta': 2,  # number of signatures needed to remove a cosignatory from multisig
			'address_additions': cosignatory_addresses
		})
	]

	# create the transaction, notice that signer account that will be turned into multisig is a signer of transaction
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'aggregate_complete_transaction_v2',
		'transactions_hash': facade.hash_embedded_transactions(embedded_transactions),
		'transactions': embedded_transactions
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	# when setting the fee for an aggregate complete, include the size of cosignatures (added later) in the fee calculation
	transaction.fee = Amount(100 * (transaction.size + len(cosignatory_key_pairs) * 104))

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'multisig account (create) transaction hash {transaction_hash}')

	# cosign transaction by all partners (this is dependent on the hash and consequently the main signature)
	for cosignatory_key_pair in cosignatory_key_pairs:
		cosignature = facade.cosign_transaction(cosignatory_key_pair, transaction)
		transaction.cosignatures.append(cosignature)

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='multisig account (create) transaction')


async def create_multisig_account_modification_modify_account(facade, signer_key_pair):
	# pylint: disable=too-many-locals
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	cosignatory_key_pairs = [facade.KeyPair(PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, i]))) for i in range(4)]
	cosignatory_addresses = [facade.network.public_key_to_address(key_pair.public_key) for key_pair in cosignatory_key_pairs]

	# multisig account modification transaction needs to be wrapped in aggregate transaction

	embedded_transactions = [
		# create a transfer from the multisig account to the primary cosignatory to cover the transaction fee
		facade.transaction_factory.create_embedded({
			'type': 'transfer_transaction_v1',
			'signer_public_key': signer_key_pair.public_key,

			'recipient_address': cosignatory_addresses[0],
			'mosaics': [
				{'mosaic_id': generate_mosaic_alias_id('symbol.xym'), 'amount': 5_000000}
			]
		}),

		facade.transaction_factory.create_embedded({
			'type': 'multisig_account_modification_transaction_v1',
			'signer_public_key': signer_key_pair.public_key,  # sender of modification transaction is multisig account

			# don't change number of cosignature needed for transactions
			'min_approval_delta': 0,
			# decrease number of signatures needed to remove a cosignatory from multisig (optional)
			'min_removal_delta': -1,
			'address_additions': [cosignatory_addresses[3]],
			'address_deletions': [cosignatory_addresses[1]]
		})
	]

	# create the transaction, notice that account that will be turned into multisig is a signer of transaction
	transaction = facade.transaction_factory.create({
		'signer_public_key': cosignatory_key_pairs[0].public_key,  # signer of the aggregate is one of the two cosignatories
		'deadline': network_time.timestamp,

		'type': 'aggregate_complete_transaction_v2',
		'transactions_hash': facade.hash_embedded_transactions(embedded_transactions),
		'transactions': embedded_transactions
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	# when setting the fee for an aggregate complete, include the size of cosignatures (added later) in the fee calculation
	transaction.fee = Amount(100 * (transaction.size + len([cosignatory_key_pairs[2], cosignatory_key_pairs[3]]) * 104))

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(cosignatory_key_pairs[0], transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'multisig account (modify) transaction hash {transaction_hash}')

	# cosign transaction by all partners (this is dependent on the hash and consequently the main signature)
	for cosignatory_key_pair in [cosignatory_key_pairs[2], cosignatory_key_pairs[3]]:
		cosignature = facade.cosign_transaction(cosignatory_key_pair, transaction)
		transaction.cosignatures.append(cosignature)

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='multisig account (modify) transaction')


async def get_mosaic_metadata(facade, signer_key_pair):
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)

	mosaic_id = generate_mosaic_id(signer_address, 123)
	scoped_metadata_key = int.from_bytes(b'rating', byteorder='little')

	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP GET request to a Symbol REST endpoint
		params = {
			'targetId': f'{mosaic_id:016X}',
			'scopedMetadataKey': f'{scoped_metadata_key:016X}'
		}
		async with session.get(f'{SYMBOL_API_ENDPOINT}/metadata', params=params) as response:
			# wait for the (JSON) response
			response_json = await response.json()

			print(json.dumps(response_json, indent=4))
			return response_json

# endregion


# region account multisig management transactions (bonded)

async def create_hash_lock(facade, signer_key_pair, bonded_transaction_hash):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'hash_lock_transaction_v1',
		'mosaic': {'mosaic_id': generate_mosaic_alias_id('symbol.xym'), 'amount': 10_000000},

		'duration': 100,
		'hash': bonded_transaction_hash
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'hash lock transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='hash lock transaction')


async def create_multisig_account_modification_new_account_bonded(facade, signer_key_pair):
	# pylint: disable=too-many-locals
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create cosignatory key pairs, where each cosignatory will be required to cosign initial modification
	# (they are insecurely deterministically generated for the benefit of related tests)
	cosignatory_key_pairs = [facade.KeyPair(PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, i]))) for i in range(3)]
	cosignatory_addresses = [facade.network.public_key_to_address(key_pair.public_key) for key_pair in cosignatory_key_pairs]

	embedded_transactions = [
		facade.transaction_factory.create_embedded({
			'type': 'multisig_account_modification_transaction_v1',
			'signer_public_key': signer_key_pair.public_key,

			'min_approval_delta': 2,  # number of signatures required to make any transaction
			'min_removal_delta': 2,  # number of signatures needed to remove a cosignatory from multisig
			'address_additions': cosignatory_addresses
		})
	]

	# create the transaction, notice that signer account that will be turned into multisig is a signer of transaction
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'aggregate_bonded_transaction_v2',
		'transactions_hash': facade.hash_embedded_transactions(embedded_transactions),
		'transactions': embedded_transactions
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	# when setting the fee for an aggregate bonded, include the size of cosignatures (added later) in the fee calculation
	transaction.fee = Amount(100 * (transaction.size + len(cosignatory_addresses) * 104))

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'multisig account modification bonded (new) {transaction_hash}')

	# print the signed transaction, including its signature
	print(transaction)

	# create a hash lock transaction to allow the network to collect cosignaatures for the aggregate
	await create_hash_lock(facade, signer_key_pair, transaction_hash)

	# submit the partial (bonded) transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions/partial', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions/partial: {response_json}')

		# wait for the partial transaction to be cached by the network
		await wait_for_transaction_status(transaction_hash, 'partial', transaction_description='bonded aggregate transaction')

		# submit the (detached) cosignatures to the network
		for cosignatory_key_pair in cosignatory_key_pairs:
			cosignature = facade.cosign_transaction(cosignatory_key_pair, transaction, True)
			cosignature_json_payload = json.dumps({
				'version': str(cosignature.version),
				'signerPublicKey': str(cosignature.signer_public_key),
				'signature': str(cosignature.signature),
				'parentHash': str(cosignature.parent_hash)
			})
			print(cosignature_json_payload)

			# initiate a HTTP PUT request to a Symbol REST endpoint
			async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions/cosignature', json=json.loads(cosignature_json_payload)) as response:
				response_json = await response.json()
				print(f'/transactions/cosignature: {response_json}')

	# wait for the partial transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='bonded aggregate transaction')

# endregion


# region transaction proof

async def _spam_transactions(facade, signer_key_pair, count):
	# create a deterministic recipient (it insecurely deterministically generated for the benefit of related tests)
	recipient_address = facade.network.public_key_to_address(PublicKey(signer_key_pair.public_key.bytes[:-4] + bytes([0, 0, 0, 0])))
	print(f'recipient: {recipient_address}')

	for i in range(1, count + 1):
		# derive the signer's address
		signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
		print(f'creating transaction with signer {signer_address}')

		# get the current network time from the network, and set the transaction deadline two hours in the future
		network_time = await get_network_time()
		network_time = network_time.add_hours(2)

		transaction = facade.transaction_factory.create({
			'signer_public_key': signer_key_pair.public_key,
			'deadline': network_time.timestamp,

			'type': 'transfer_transaction_v1',
			'recipient_address': recipient_address,
			'mosaics': [
				{'mosaic_id': generate_mosaic_alias_id('symbol.xym'), 'amount': i},  # send some xym
			],
		})

		# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
		transaction.fee = Amount(100 * transaction.size)

		# sign the transaction and attach its signature
		signature = facade.sign_transaction(signer_key_pair, transaction)
		facade.transaction_factory.attach_signature(transaction, signature)

		# hash the transaction (this is dependent on the signature)
		transaction_hash = facade.hash_transaction(transaction)
		print(f'transfer transaction hash {transaction_hash}')

		# finally, construct the over wire payload
		json_payload = facade.transaction_factory.attach_signature(transaction, signature)

		# print the signed transaction, including its signature
		print(transaction)

		# submit the transaction to the network
		async with ClientSession(raise_for_status=True) as session:
			# initiate a HTTP PUT request to a Symbol REST endpoint
			async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
				response_json = await response.json()
				print(f'/transactions: {response_json}')


async def prove_confirmed_transaction(facade, signer_key_pair):
	await _spam_transactions(facade, signer_key_pair, 10)

	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create a deterministic recipient (it insecurely deterministically generated for the benefit of related tests)
	recipient_address = facade.network.public_key_to_address(PublicKey(signer_key_pair.public_key.bytes[:-4] + bytes([0, 0, 0, 0])))
	print(f'recipient: {recipient_address}')

	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'transfer_transaction_v1',
		'recipient_address': recipient_address,
		'mosaics': [
			{'mosaic_id': generate_mosaic_alias_id('symbol.xym'), 'amount': 1_000000},  # send 1 of XYM to recipient
		],
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	transaction.fee = Amount(100 * transaction.size)

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'transfer transaction hash {transaction_hash}')

	# finally, construct the over wire payload
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# print the signed transaction, including its signature
	print(transaction)

	# submit the transaction to the network
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP PUT request to a Symbol REST endpoint
		async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
			response_json = await response.json()
			print(f'/transactions: {response_json}')

	# wait for the transaction to be confirmed
	await wait_for_transaction_status(transaction_hash, 'confirmed', transaction_description='transfer transaction')

	# create a connection to a node
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP GET request to a Symbol REST endpoint to get information about the confirmed transaction
		async with session.get(f'{SYMBOL_API_ENDPOINT}/transactions/confirmed/{transaction_hash}') as response:
			# extract the confirmed block height
			response_json = await response.json()
			confirmed_block_height = int(response_json['meta']['height'])
			print(f'confirmed block height: {confirmed_block_height}')

		# initiate a HTTP GET request to a Symbol REST endpoint to get information about the confirming block
		async with session.get(f'{SYMBOL_API_ENDPOINT}/blocks/{confirmed_block_height}') as response:
			# extract the block transactions hash
			response_json = await response.json()
			block_transactions_hash = Hash256(response_json['block']['transactionsHash'])
			print(f'block transactions hash: {block_transactions_hash}')

		# initiate a HTTP GET request to a Symbol REST endpoint to get a transaction merkle proof
		print(f'{SYMBOL_API_ENDPOINT}/blocks/{confirmed_block_height}/transactions/{transaction_hash}/merkle')
		async with session.get(f'{SYMBOL_API_ENDPOINT}/blocks/{confirmed_block_height}/transactions/{transaction_hash}/merkle') as response:
			# extract the merkle proof path and transform it into format expected by sdk
			response_json = await response.json()
			print(response_json)
			merkle_proof_path = list(map(
				lambda part: MerklePart(Hash256(part['hash']), 'left' == part['position']),
				response_json['merklePath']))
			print(merkle_proof_path)

			# perform the proof
			if prove_merkle(transaction_hash, merkle_proof_path, block_transactions_hash):
				print(f'transaction {transaction_hash} is proven to be in block {confirmed_block_height}')
			else:
				raise RuntimeError(f'transaction {transaction_hash} is NOT proven to be in block {confirmed_block_height}')


async def prove_xym_mosaic_state(facade, _):  # pylint: disable=too-many-locals
	# determine the network currency mosaic
	network_currency_id = await get_network_currency()
	network_currency_id_formatted = f'{network_currency_id:08X}'

	# get the current network height
	start_network_height = await get_network_height()

	# look up the properties of the network currency mosaic
	mosaic_properties_json = (await get_mosaic_properties(network_currency_id_formatted))['mosaic']
	print(mosaic_properties_json)

	# serialize and hash the mosaic properties
	writer = BufferWriter()
	writer.write_int(int(mosaic_properties_json['version']), 2)
	writer.write_int(int(mosaic_properties_json['id'], 16), 8)
	writer.write_int(int(mosaic_properties_json['supply']), 8)
	writer.write_int(int(mosaic_properties_json['startHeight']), 8)
	writer.write_bytes(facade.Address(unhexlify(mosaic_properties_json['ownerAddress'])).bytes)
	writer.write_int(int(mosaic_properties_json['revision']), 4)
	writer.write_int(int(mosaic_properties_json['flags']), 1)
	writer.write_int(int(mosaic_properties_json['divisibility']), 1)
	writer.write_int(int(mosaic_properties_json['duration']), 8)
	mosaic_hashed_value = Hash256(sha3.sha3_256(writer.buffer).digest())
	print(f'mosaic hashed value: {mosaic_hashed_value}')

	# hash the mosaic id to get the key
	writer = BufferWriter()
	writer.write_int(int(mosaic_properties_json['id'], 16), 8)
	mosaic_encoded_key = Hash256(sha3.sha3_256(writer.buffer).digest())
	print(f'mosaic encoded key: {mosaic_encoded_key}')

	# create a connection to a node
	async with ClientSession(raise_for_status=True) as session:
		# initiate a HTTP GET request to a Symbol REST endpoint to get information about the last block
		async with session.get(f'{SYMBOL_API_ENDPOINT}/blocks/{start_network_height}') as response:
			# extract the sub cache merkle roots and the stateHash
			response_json = await response.json()
			state_hash = Hash256(response_json['block']['stateHash'])
			subcache_merkle_roots = [Hash256(root) for root in response_json['meta']['stateHashSubCacheMerkleRoots']]
			print(f'state hash: {state_hash}')
			print(f'subcache merkle roots: {subcache_merkle_roots}')

		# initiate a HTTP GET request to a Symbol REST endpoint to get a state hash merkle proof
		async with session.get(f'{SYMBOL_API_ENDPOINT}/mosaics/{network_currency_id_formatted}/merkle') as response:
			# extract the merkle proof and transform it into format expected by sdk
			response_json = await response.json()
			merkle_proof_path = deserialize_patricia_tree_nodes(unhexlify(response_json['raw']))

			# perform the proof
			proof_result = prove_patricia_merkle(
				mosaic_encoded_key,
				mosaic_hashed_value,
				merkle_proof_path,
				state_hash,
				subcache_merkle_roots)
			print(f'mosaic {network_currency_id_formatted} proof concluded with {proof_result}')

	end_network_height = await get_network_height()

	if start_network_height != end_network_height:
		print('blockchain changed during test, result of PATH_MISMATCH is expected')
	else:
		print('blockchain did NOT change during test, result of VALID_POSITIVE is expected')

# endregion


# region websockets

async def read_websocket_block(_, _1):
	# connect to websocket endpoint
	async with connect(SYMBOL_WEBSOCKET_ENDPOINT) as websocket:
		# extract user id from connect response
		response_json = json.loads(await websocket.recv())
		user_id = response_json['uid']
		print(f'established websocket connection with user id {user_id}')

		# subscribe to block messages
		subscribe_message = {'uid': user_id, 'subscribe': 'block'}
		await websocket.send(json.dumps(subscribe_message))
		print('subscribed to block messages')

		# wait for the next block message
		response_json = json.loads(await websocket.recv())
		print(f'received message with topic: {response_json["topic"]}')
		print(f'received block at height {response_json["data"]["block"]["height"]} with hash {response_json["data"]["meta"]["hash"]}')
		print(response_json['data']['block'])

		# unsubscribe from block messages
		unsubscribe_message = {'uid': user_id, 'unsubscribe': 'block'}
		await websocket.send(json.dumps(unsubscribe_message))
		print('unsubscribed from block messages')


async def read_websocket_transaction_flow(facade, signer_key_pair):
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# connect to websocket endpoint
	async with connect(SYMBOL_WEBSOCKET_ENDPOINT) as websocket:
		# extract user id from connect response
		response_json = json.loads(await websocket.recv())
		user_id = response_json['uid']
		print(f'established websocket connection with user id {user_id}')

		# subscribe to transaction messages associated with the signer
		# * confirmedAdded - transaction was confirmed
		# * unconfirmedAdded - transaction was added to unconfirmed cache
		# * unconfirmedRemoved - transaction was removed from unconfirmed cache
		# notice that all of these are scoped to a single address
		channel_names = ('confirmedAdded', 'unconfirmedAdded', 'unconfirmedRemoved')
		for channel_name in channel_names:
			subscribe_message = {'uid': user_id, 'subscribe': f'{channel_name}/{signer_address}'}
			await websocket.send(json.dumps(subscribe_message))
			print(f'subscribed to {channel_name} messages')

		# send two transactions
		unconfirmed_transactions_count = 2
		await _spam_transactions(facade, signer_key_pair, unconfirmed_transactions_count)

		# read messages from the websocket as the transactions move from unconfirmed to confirmed
		# notice that "added" messages contain the full transaction payload whereas "removed" messages only contain the hash
		# expected progression is unconfirmedAdded, unconfirmedRemoved, confirmedAdded
		while True:
			response_json = json.loads(await websocket.recv())
			topic = response_json['topic']
			print(f'received message with topic {topic} for transaction {response_json["data"]["meta"]["hash"]}')
			if topic.startswith('confirmedAdded'):
				unconfirmed_transactions_count -= 1
				if 0 == unconfirmed_transactions_count:
					print('all transactions confirmed')
					break

		# unsubscribe from transaction messages
		for channel_name in channel_names:
			unsubscribe_message = {'uid': user_id, 'unsubscribe': f'{channel_name}/{signer_address}'}
			await websocket.send(json.dumps(unsubscribe_message))
			print(f'unsubscribed from {channel_name} messages')


async def read_websocket_transaction_error(facade, signer_key_pair):
	# pylint: disable=too-many-locals
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# connect to websocket endpoint
	async with connect(SYMBOL_WEBSOCKET_ENDPOINT) as websocket:
		# extract user id from connect response
		response_json = json.loads(await websocket.recv())
		user_id = response_json['uid']
		print(f'established websocket connection with user id {user_id}')

		# subscribe to transaction messages associated with the signer
		# * status - transaction was rejected
		# notice that all of these are scoped to a single address
		subscribe_message = {'uid': user_id, 'subscribe': f'status/{signer_address}'}
		await websocket.send(json.dumps(subscribe_message))
		print('subscribed to status messages')

		# create a deterministic recipient (it insecurely deterministically generated for the benefit of related tests)
		recipient_address = facade.network.public_key_to_address(PublicKey(signer_key_pair.public_key.bytes[:-4] + bytes([0, 0, 0, 0])))
		print(f'recipient: {recipient_address}')

		# derive the signer's address
		signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
		print(f'creating transaction with signer {signer_address}')

		# get the current network time from the network, and set the transaction deadline two hours in the future
		network_time = await get_network_time()
		network_time = network_time.add_hours(2)

		# prepare transaction that will be rejected (insufficient balance)
		transaction = facade.transaction_factory.create({
			'signer_public_key': signer_key_pair.public_key,
			'deadline': network_time.timestamp,

			'type': 'transfer_transaction_v1',
			'recipient_address': recipient_address,
			'mosaics': [
				{'mosaic_id': generate_mosaic_alias_id('symbol.xym'), 'amount': 1000_000000}
			],
		})

		# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
		transaction.fee = Amount(100 * transaction.size)

		# sign the transaction and attach its signature
		signature = facade.sign_transaction(signer_key_pair, transaction)
		facade.transaction_factory.attach_signature(transaction, signature)

		# hash the transaction (this is dependent on the signature)
		transaction_hash = facade.hash_transaction(transaction)
		print(f'transfer transaction hash {transaction_hash}')

		# finally, construct the over wire payload
		json_payload = facade.transaction_factory.attach_signature(transaction, signature)

		# print the signed transaction, including its signature
		print(transaction)

		# submit the transaction to the network
		async with ClientSession(raise_for_status=True) as session:
			# initiate a HTTP PUT request to a Symbol REST endpoint
			async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions', json=json.loads(json_payload)) as response:
				response_json = await response.json()
				print(f'/transactions: {response_json}')

		# read messages from the websocket as the transactions move from unconfirmed to confirmed
		# notice that "added" messages contain the full transaction payload whereas "removed" messages only contain the hash
		# expected progression is unconfirmedAdded, unconfirmedRemoved, confirmedAdded
		response_json = json.loads(await websocket.recv())
		print(f'received message with topic: {response_json["topic"]}')
		print(f'transaction {response_json["data"]["hash"]} was rejected with {response_json["data"]["code"]}')

		# unsubscribe from status messages
		unsubscribe_message = {'uid': user_id, 'unsubscribe': f'status/{signer_address}'}
		await websocket.send(json.dumps(unsubscribe_message))
		print('unsubscribed from status messages')


async def read_websocket_transaction_bonded_flow(facade, signer_key_pair):
	# pylint: disable=too-many-locals
	# derive the signer's address
	signer_address = facade.network.public_key_to_address(signer_key_pair.public_key)
	print(f'creating transaction with signer {signer_address}')

	# get the current network time from the network, and set the transaction deadline two hours in the future
	network_time = await get_network_time()
	network_time = network_time.add_hours(2)

	# create cosignatory key pairs, where each cosignatory will be required to cosign initial modification
	# (they are insecurely deterministically generated for the benefit of related tests)
	cosignatory_key_pairs = [facade.KeyPair(PrivateKey(signer_key_pair.private_key.bytes[:-4] + bytes([0, 0, 0, i]))) for i in range(3)]
	cosignatory_addresses = [facade.network.public_key_to_address(key_pair.public_key) for key_pair in cosignatory_key_pairs]

	embedded_transactions = [
		facade.transaction_factory.create_embedded({
			'type': 'multisig_account_modification_transaction_v1',
			'signer_public_key': signer_key_pair.public_key,

			'min_approval_delta': 2,  # number of signatures required to make any transaction
			'min_removal_delta': 2,  # number of signatures needed to remove a cosignatory from multisig
			'address_additions': cosignatory_addresses
		})
	]

	# create the transaction, notice that signer account that will be turned into multisig is a signer of transaction
	transaction = facade.transaction_factory.create({
		'signer_public_key': signer_key_pair.public_key,
		'deadline': network_time.timestamp,

		'type': 'aggregate_bonded_transaction_v2',
		'transactions_hash': facade.hash_embedded_transactions(embedded_transactions),
		'transactions': embedded_transactions
	})

	# set the maximum fee that the signer will pay to confirm the transaction; transactions bidding higher fees are generally prioritized
	# when setting the fee for an aggregate bonded, include the size of cosignatures (added later) in the fee calculation
	transaction.fee = Amount(100 * (transaction.size + len(cosignatory_addresses) * 104))

	# sign the transaction and attach its signature
	signature = facade.sign_transaction(signer_key_pair, transaction)
	json_payload = facade.transaction_factory.attach_signature(transaction, signature)

	# hash the transaction (this is dependent on the signature)
	transaction_hash = facade.hash_transaction(transaction)
	print(f'multisig account modification bonded (new) {transaction_hash}')

	# print the signed transaction, including its signature
	print(transaction)

	# create a hash lock transaction to allow the network to collect cosignaatures for the aggregate
	await create_hash_lock(facade, signer_key_pair, transaction_hash)

	# connect to websocket endpoint
	async with connect(SYMBOL_WEBSOCKET_ENDPOINT) as websocket:
		# extract user id from connect response
		response_json = json.loads(await websocket.recv())
		user_id = response_json['uid']
		print(f'established websocket connection with user id {user_id}')

		# subscribe to transaction messages associated with the signer
		# * confirmedAdded - transaction was confirmed
		# * unconfirmedAdded - transaction was added to unconfirmed cache
		# * unconfirmedRemoved - transaction was removed from unconfirmed cache
		# * partialAdded - transaction was added to partial cache
		# * partialRemoved - transaction was removed from partial cache
		# * cosignature - cosignature was added
		# notice that all of these are scoped to a single address
		channel_names = ('confirmedAdded', 'unconfirmedAdded', 'unconfirmedRemoved', 'partialAdded', 'partialRemoved', 'cosignature')
		for channel_name in channel_names:
			subscribe_message = {'uid': user_id, 'subscribe': f'{channel_name}/{signer_address}'}
			await websocket.send(json.dumps(subscribe_message))
			print(f'subscribed to {channel_name} messages')

		# submit the partial (bonded) transaction to the network
		async with ClientSession(raise_for_status=True) as session:
			# initiate a HTTP PUT request to a Symbol REST endpoint
			async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions/partial', json=json.loads(json_payload)) as response:
				response_json = await response.json()
				print(f'/transactions/partial: {response_json}')

		# read messages from the websocket as the transaction moves from partial to unconfirmed to confirmed
		# notice that "added" messages contain the full transaction payload whereas "removed" messages only contain the hash
		# expected progression is
		# * partialAdded, cosignature, cosignature, cosignature, partialRemoved
		# * unconfirmedAdded, unconfirmedRemoved
		# * confirmedAdded
		while True:
			response_json = json.loads(await websocket.recv())
			topic = response_json['topic']
			if topic.startswith('cosignature'):
				cosignature = response_json['data']
				print(f'received cosignature for transaction {cosignature["parentHash"]} from {cosignature["signerPublicKey"]}')
			else:
				print(f'received message with topic {topic} for transaction {response_json["data"]["meta"]["hash"]}')

			if topic.startswith('partialAdded'):
				async with ClientSession(raise_for_status=True) as session:
					# submit the (detached) cosignatures to the network
					for cosignatory_key_pair in cosignatory_key_pairs:
						cosignature = facade.cosign_transaction(cosignatory_key_pair, transaction, True)
						cosignature_json_payload = json.dumps({
							'version': str(cosignature.version),
							'signerPublicKey': str(cosignature.signer_public_key),
							'signature': str(cosignature.signature),
							'parentHash': str(cosignature.parent_hash)
						})
						print(cosignature_json_payload)

						# initiate a HTTP PUT request to a Symbol REST endpoint
						async with session.put(f'{SYMBOL_API_ENDPOINT}/transactions/cosignature', json=json.loads(cosignature_json_payload)) as response:
							response_json = await response.json()
							print(f'/transactions/cosignature: {response_json}')

			if topic.startswith('confirmedAdded'):
				print('transaction confirmed')
				break

		# unsubscribe from transaction messages
		for channel_name in channel_names:
			unsubscribe_message = {'uid': user_id, 'unsubscribe': f'{channel_name}/{signer_address}'}
			await websocket.send(json.dumps(unsubscribe_message))
			print(f'unsubscribed from {channel_name} messages')

# endregion


def print_banner(name):
	console_width = shutil.get_terminal_size()[0]
	print('*' * console_width)

	name_padding = ' ' * ((console_width - len(name)) // 2 - 4)
	name_trailing_whitespace = ' ' if 0 == (console_width - len(name)) % 2 else '  '
	print(f'***{name_padding} {name}{name_trailing_whitespace}{name_padding}***')

	print('*' * console_width)


def run_offline_examples(facade):
	functions = [
		create_random_account,
		create_random_bip32_account,
		create_voting_key_file
	]
	for func in functions:
		print_banner(func.__qualname__)
		func(facade)


async def run_network_query_examples():
	functions = [
		get_network_time,
		get_maximum_supply,
		get_total_supply,
		get_circulating_supply,
		get_network_height,
		get_network_finalized_height
	]
	for func in functions:
		print_banner(func.__qualname__)
		await func()


async def run_account_query_examples():
	functions = [
		get_account_balance
	]
	for func in functions:
		print_banner(func.__qualname__)
		await func()


async def run_transaction_examples(facade, group_filter=None):
	function_groups = [
		('CREATE (BASIC)', [
			create_transfer_with_encrypted_message,
			create_account_metadata_new,
			create_account_metadata_modify,

			create_secret_lock,
			create_secret_proof,

			create_namespace_registration_root,
			create_namespace_registration_child,
			create_namespace_metadata_new,
			create_namespace_metadata_modify,

			create_mosaic_definition_new,
			create_mosaic_definition_modify,
			create_mosaic_supply,
			create_mosaic_transfer,
			create_mosaic_revocation,
			create_mosaic_atomic_swap,

			create_mosaic_metadata_new,
			create_mosaic_metadata_cosigned_1,
			create_mosaic_metadata_cosigned_2,
			get_mosaic_metadata,

			create_global_mosaic_restriction_new,
			create_address_mosaic_restriction_1,
			create_address_mosaic_restriction_2,
			create_address_mosaic_restriction_3,
			create_global_mosaic_restriction_modify
		]),
		('CREATE (LINKS)', [
			create_account_key_link,
			create_vrf_key_link,
			create_voting_key_link,
			create_node_key_link,

			create_harvesting_delegation_message,

			create_account_key_unlink,
			create_vrf_key_unlink,
			create_voting_key_unlink,
			create_node_key_unlink
		]),
		('CREATE MULTISIG (COMPLETE)', [
			create_multisig_account_modification_new_account,
			create_multisig_account_modification_modify_account
		]),
		('CREATE MULTISIG (BONDED)', [
			create_multisig_account_modification_new_account_bonded
		]),
		('PROOFS', [
			prove_confirmed_transaction,
			prove_xym_mosaic_state
		]),
		('WEBSOCKETS', [
			read_websocket_block,
			read_websocket_transaction_flow,
			read_websocket_transaction_error
		]),
		('WEBSOCKETS (PARTIAL)', [
			read_websocket_transaction_bonded_flow
		])
	]
	for (group_name, functions) in function_groups:
		if group_filter and group_filter != group_name:
			continue

		print_banner(f'CREATING SIGNER ACCOUNT FOR TRANSACTION EXAMPLES - {group_name}')

		# create a signing key pair that will be used to sign the created transaction(s) in this group
		signer_key_pair = await create_account_with_tokens_from_faucet(facade)

		for func in functions:
			print_banner(func.__qualname__)
			await func(facade, signer_key_pair)


async def main():
	facade = SymbolFacade('testnet')

	run_offline_examples(facade)
	await run_network_query_examples()
	await run_account_query_examples()
	await run_transaction_examples(facade)

	print_banner('FIN')


if __name__ == '__main__':
	asyncio.run(main())
