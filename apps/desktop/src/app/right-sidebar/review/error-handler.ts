import { notifyError } from '@/store/notifications'
import { captureReviewOwner } from '@/store/review'

/** Capture at dispatch, before a rejection can cross the store/caller boundary. */
export function reviewErrorHandler(title: string): (error: unknown) => void {
  const isCurrent = captureReviewOwner()

  return error => {
    if (isCurrent()) {
      notifyError(error, title)
    }
  }
}
