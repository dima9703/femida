#!/usr/bin/env python3

import pymongo.errors
import argparse
import os
import cv2
import logging
import persistqueue
import datetime
import femida_detect

parser = argparse.ArgumentParser()
STATUS_IN_PROGRESS = 'ICR in progress'
STATUS_ERROR = 'ICR errored'
STATUS_COMPLETE = 'ICR complete'
STATUS_IN_QUEUE = 'in queue for ICR'
STATUS_PARTIALLY_COMPLETE = 'ICR partially complete'
ANSWERS = 'answers'
PDFS = 'pdfs'
DB = 'femida'

parser.add_argument('--host', type=str, default='localhost')
parser.add_argument('--port', type=int, default=27017)
parser.add_argument('--log-to', type=str,
                    help='path to logfile')
parser.add_argument('--root', type=str, required=True,
                    help='path to local queue')
parser.add_argument('--model-path', type=str, required=True)
parser.add_argument('--save-path', type=str, default='/media/EMSCH_tests_ICR/icr_results/')
parser.add_argument('--cuda', action='store_true')
parser.add_argument('--debug', action='store_true')


def main(args):
    logger = logging.getLogger('consumer :: answers')
    formatter = logging.Formatter('%(asctime)s :: %(name)s :: %(levelname)s - %(message)s')
    if args.log_to is not None:
        handle = logging.FileHandler(args.log_to)
    else:
        handle = logging.StreamHandler()
    handle.setFormatter(formatter)
    logger.addHandler(handle)
    if args.debug:
        logger.setLevel(logging.DEBUG)
    logger.info(f'storing icr results here: {args.save_path}')
    os.makedirs(args.save_path, exist_ok=True)
    queue = persistqueue.SQLiteQueue(
        os.path.join(args.root, 'answers'))
    logger.info(f'Opened Answers Queue {queue.path}')
    pdf_status = persistqueue.PDict(
        os.path.join(args.root, 'pdf.status'), 'pdf'
    )
    logger.info(f'Opened PDF Status Dict {pdf_status.path}')
    logger.info(f'Connecting to {args.host}:{args.port} with provided credentials')
    conn = pymongo.MongoClient(
        host=args.host,
        port=args.port,
        username=os.environ.get('MONGO_USER'),
        password=os.environ.get('MONGO_PASSWORD')
    )
    pdfs = conn[DB][PDFS]
    answers = conn[DB][ANSWERS]
    logger.info(f'Loading model from {args.model_path}')
    predict = femida_detect.detect.eval.load(args.model_path, ('cuda' if args.cuda else 'cpu'))
    logger.info(f'Started listening')
    while True:
        task = queue.get()
        try:
            logger.info(f"Got new task _id={task['_id']} :: {task['i']}")
            image = cv2.imread(task['imagef'])
            test_results = dict.fromkeys(list(map(str, range(1, 41))), '')
            test_updates = [{
                'sessoin_id': 'icr', 'updates': {}, 'date': datetime.datetime.now()}
            ]
            result = dict(
                personal=[],
                UUID=task['UUID'],
                requested_manual=[],
                manual_checks=[],
                test_results=test_results,
                test_updates=test_updates
            )
            logger.debug(f"Task _id={task['_id']} :: {task['i']} :: CroppedAnswers.from_raw")
            cropped = femida_detect.imgparse.CroppedAnswers.from_raw(image)
            img_fio = os.path.join(args.save_path, f"{task['UUID']}__{task['i']}_fio.jpg")
            orig = cropped.personal
            h, w, _ = orig.shape
            cv2.imwrite(
                img_fio,
                cv2.resize(orig, (1000 * h // w, 1000))
            )
            result['img_fio'] = img_fio
            logger.debug(f"Task _id={task['_id']} :: {task['i']} :: predict(cropped)")
            predictions = predict(cropped)
            logger.debug(f"Task _id={task['_id']} :: {task['i']} :: cropped.plot_predicted(predictions)")
            inpainted = cropped.plot_predicted(predictions, only_answers=True)
            img_test_form = os.path.join(args.save_path, f"{task['UUID']}__{task['i']}_test_form.jpg")
            h, w, _ = inpainted.shape
            cv2.imwrite(
                img_test_form,
                cv2.resize(inpainted, (1000 * h // w, 1000))
            )
            result['img_test_form'] = img_test_form
            for (j, letter), pred in zip(
                    cropped.get_labels(),
                    predictions
            ):
                if pred:
                    test_results[str(j)] += letter
            logger.info(f"Task _id={task['_id']} :: {task['i']} :: status :: normal")
            result.update(status='normal')
            pdf_status[task['UUID']] -= 1
            try:
                # ok, try to update pdf database
                current_pdf_status = pdf_status[task['UUID']]
                current_pdf_err_status = pdf_status[task['UUID'] + '__err']
                if current_pdf_status == 0:
                    free_pdf_status = True
                    logger.debug(f"Task _id={task['_id']} :: {task['i']} is the last one for UUID")
                    if current_pdf_err_status == 0:
                        logger.info(f"Task _id={task['_id']} :: status :: {STATUS_COMPLETE}")
                        pdfs.update_one(
                            {'_id': task['_id']},
                            {'$set': {'status': STATUS_COMPLETE}}
                        )
                    elif current_pdf_err_status > 0:
                        logger.info(f"Task _id={task['_id']} :: status :: {STATUS_PARTIALLY_COMPLETE}")
                        pdfs.update_one(
                            {'_id': task['_id']},
                            {'$set': {'status': STATUS_PARTIALLY_COMPLETE}}
                        )
                else:
                    free_pdf_status = False
                # we succeeded to update database, try to update answers database
                # this order makes pymongo errors recoverable
                answers.insert(result)
                if free_pdf_status:
                    del pdf_status[task['UUID']]
                    del pdf_status[task['UUID'] + '__err']
                # clean up
                os.unlink(task['imagef'])
            except pymongo.errors.PyMongoError as e:
                # We get a recoverable error with database, restart can help
                logger.error(f"Task _id={task['_id']} :: recoverable (restart) :: %s", e)
                queue.put(task)
                pdf_status[task['UUID']] += 1
                break
        # top level error we only care about
        except cv2.error as e:
            result = dict(
                personal=[],
                UUID=task['UUID'],
                requested_manual=[],
                manual_checks=[],
                test_results=[],
                test_updates=[],
                status='error'
            )
            try:
                logger.error(f"Task _id={task['_id']} :: {task['i']} :: status :: error :: %s", e)
                answers.insert(result)
                pdf_status[task['UUID']] -= 1
                pdf_status[task['UUID'] + '__err'] += 1
                os.unlink(task['imagef'])
            except pymongo.errors.PyMongoError as e:
                logger.error(f"Task _id={task['_id']} :: {task['i']} :: recoverable (restart) :: %s", e)
                queue.put(task)
                break


if __name__ == '__main__':
    main(parser.parse_args())
